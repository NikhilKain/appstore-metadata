#!/usr/bin/env node
/**
 * Builds `data/modules.json` for the Vyxel metadata CDN.
 *
 * This is the work the app used to do on every visit to the Modules screen: two
 * curated indexes, a fan-out over a hundred-odd individual `module.prop` files, and
 * up to twelve paged GitHub organisation listings. On a phone that is fifteen-odd
 * seconds cold and a real bite out of GitHub's 60-requests-an-hour budget. Done once
 * a day on a runner it costs nothing, and the app's whole module catalogue becomes a
 * single conditional GET that is usually a 304.
 *
 * Usage, from the metadata repo's workflow:
 *
 *     node build-modules.mjs > data/modules.json
 *
 * Set GITHUB_TOKEN in the environment — the org listings are ~12 requests and the
 * unauthenticated ceiling is 60/hour shared with everything else in the workflow.
 *
 * Exits non-zero without writing anything if it cannot produce a plausible catalogue,
 * so a bad run leaves yesterday's file in place rather than replacing it with a stub.
 */

const TOKEN = process.env.GITHUB_TOKEN || "";
const UA = "VyxelMetadataBuild/1.0 (+https://github.com/NikhilKain)";

/** Below this something went wrong upstream; keep the previous file. */
const MIN_PLAUSIBLE = 200;

const ghHeaders = {
  "User-Agent": UA,
  Accept: "application/vnd.github+json",
  ...(TOKEN ? { Authorization: `Bearer ${TOKEN}` } : {}),
};

async function getJson(url, headers = { "User-Agent": UA }) {
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

async function getText(url) {
  const res = await fetch(url, { headers: { "User-Agent": UA } });
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.text();
}

/**
 * Which root manager a module plugs into, read out of its own text.
 *
 * No index publishes this as a field. Order matters: a module mentioning both Zygisk
 * and Magisk is a Zygisk module saying what it needs, not a Magisk one. Kept
 * byte-identical in intent to `moduleFamily` in the app so a module does not change
 * family depending on which side classified it.
 */
function family(...text) {
  const hay = text.filter(Boolean).join(" ").toLowerCase();
  if (hay.includes("lsposed") || hay.includes("xposed")) return "LSPosed";
  if (hay.includes("zygisk")) return "Zygisk";
  if (hay.includes("kernelsu") || hay.includes("kernel su")) return "KernelSU";
  return "Magisk";
}

/** Mirrors `readableModuleName` in the app: recover a title from an id. */
function readableName(published, moduleId) {
  const given = (published || "").trim();
  const looksLikeId =
    !given ||
    given === moduleId ||
    (!/\s/.test(given) && ((given.match(/\./g) || []).length >= 2 || given.includes("/")));
  if (!looksLikeId) return given;

  const tail = (moduleId.split("/").pop() || "").trim();
  if (!tail) return moduleId;

  const segments = tail.split(".").filter(Boolean);
  const meaningful =
    segments.length <= 1
      ? segments
      : segments[segments.length - 1].length <= 4
        ? segments.slice(-2)
        : [segments[segments.length - 1]];

  const words = meaningful
    .flatMap((seg) =>
      seg
        .split(/[_\- ]/)
        .filter(Boolean)
        .flatMap((chunk) => chunk.split(/(?<=[a-z0-9])(?=[A-Z])/)),
    )
    .filter(Boolean);

  if (!words.length) return tail;
  return words
    .map((w) => (w.length > 1 && w === w.toUpperCase() ? w : w[0].toUpperCase() + w.slice(1)))
    .join(" ");
}

function parseProps(text) {
  const out = {};
  for (const line of text.split(/\r?\n/)) {
    const t = line.trim();
    if (!t || t.startsWith("#")) continue;
    const i = t.indexOf("=");
    if (i <= 0) continue;
    out[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
  return out;
}

/** Runs `jobs` with at most `limit` in flight. */
async function pooled(items, limit, fn) {
  const out = [];
  let cursor = 0;
  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (cursor < items.length) {
        const index = cursor++;
        try {
          const value = await fn(items[index]);
          if (value) out.push(value);
        } catch {
          // One module failing is one module missing, not a failed build.
        }
      }
    }),
  );
  return out;
}

// ── sources ─────────────────────────────────────────────────────────────────

/** MMRL: one document with full metadata and version history. */
async function googlers() {
  const root = await getJson(
    "https://raw.githubusercontent.com/Googlers-Repo/gmr/master/json/modules.json",
  );
  const entries = root.modules || root || [];
  return entries.flatMap((e) => {
    if (!e?.id) return [];
    const versions = [...(e.versions || [])].sort(
      (a, b) => (b.versionCode || 0) - (a.versionCode || 0),
    );
    const latest = versions[0] || {};
    const summary = e.description || "";
    return [{
      id: e.id,
      name: readableName(e.name, e.id),
      summary: summary.slice(0, 240),
      version: (e.version || latest.version || "").replace(/^v/, ""),
      author: (e.author || "").replace(/^@/, ""),
      // The id is part of the haystack, exactly as in the app. Without it
      // `zygisk_shamiko` — whose description is a Japanese joke and mentions no
      // manager at all — fell through to the Magisk default here while the app
      // called it Zygisk, so the same module changed family depending on which
      // side classified it.
      family: family(e.name, summary, e.id),
      source: "googlers",
      stars: e.stars || 0,
      zipUrl: latest.zipUrl || e.zipUrl || "",
      homepage: e.track?.source || e.homepage || "",
      size: e.size || latest.size || 0,
      updated: normaliseTimestamp(e.timestamp),
    }];
  });
}

/**
 * Alt Repo: a thin index, so every module needs its `module.prop` fetched.
 *
 * This is the single most expensive part of the old on-device path — and the part
 * that benefits most from being done here, once, instead of per user per visit.
 */
async function altRepo() {
  const root = await getJson(
    "https://raw.githubusercontent.com/Magisk-Modules-Alt-Repo/json/main/modules.json",
  );
  const stubs = (root.modules || []).filter((m) => m?.id);

  return pooled(stubs, 8, async (stub) => {
    let props = {};
    if (stub.prop_url) {
      try {
        props = parseProps(await getText(stub.prop_url));
      } catch {
        // Keep the module under a name derived from its id rather than dropping it.
      }
    }
    const summary = props.description || "";
    return {
      id: stub.id,
      name: readableName(props.name, stub.id),
      summary: summary.slice(0, 240),
      version: (props.version || "").replace(/^v/, ""),
      author: (props.author || "").replace(/^@/, ""),
      family: family(props.name, summary, stub.id),
      source: "magiskalt",
      stars: stub.stars || 0,
      zipUrl: stub.zip_url || "",
      homepage: `https://github.com/Magisk-Modules-Alt-Repo/${stub.id}`,
      size: 0,
      updated: normaliseTimestamp(stub.last_update),
    };
  });
}

/**
 * Every repository in a module organisation — where the volume actually is.
 *
 * The curated indexes hold a couple of hundred modules between them; the Xposed org
 * alone is over a thousand. No 1000-result ceiling, and it draws on the core rate
 * limit rather than search's much tighter one.
 */
async function org(orgName, sourceId, familyHint) {
  const out = [];
  for (let page = 1; page <= 12; page++) {
    const url =
      `https://api.github.com/orgs/${orgName}/repos` +
      `?per_page=100&page=${page}&sort=pushed&direction=desc&type=public`;
    let repos;
    try {
      repos = await getJson(url, ghHeaders);
    } catch (e) {
      process.stderr.write(`${orgName} page ${page}: ${e.message}\n`);
      break;
    }
    if (!Array.isArray(repos) || repos.length === 0) break;

    for (const repo of repos) {
      if (!repo?.name) continue;
      const summary = repo.description || "";
      // A placeholder repo with no description and nothing pushed is a dead row.
      if (repo.archived && !summary) continue;
      out.push({
        id: repo.name,
        name: readableName(repo.name, repo.name),
        summary: summary.slice(0, 240),
        // Resolved by the app on demand: a release lookup per repo would be a
        // thousand extra calls here for data most of which nobody opens.
        version: "",
        author: repo.owner?.login || orgName,
        family: familyHint || family(repo.name, summary),
        source: sourceId,
        stars: repo.stargazers_count || 0,
        zipUrl: "",
        homepage: repo.html_url || "",
        size: 0,
        updated: Date.parse(repo.pushed_at || "") || 0,
      });
    }
    if (repos.length < 100) break;
  }
  return out;
}

/** Seconds, milliseconds and floating-point seconds all appear in these indexes. */
function normaliseTimestamp(raw) {
  const n = Number(raw);
  if (!Number.isFinite(n) || n <= 0) return 0;
  return n < 100_000_000_000 ? Math.round(n * 1000) : Math.round(n);
}

// ── main ────────────────────────────────────────────────────────────────────

const results = await Promise.allSettled([
  googlers(),
  altRepo(),
  org("Xposed-Modules-Repo", "xposed", "LSPosed"),
  org("Magisk-Modules-Repo", "magisklegacy", "Magisk"),
]);

for (const [i, r] of results.entries()) {
  if (r.status === "rejected") process.stderr.write(`source ${i} failed: ${r.reason}\n`);
}

const all = results.flatMap((r) => (r.status === "fulfilled" ? r.value : []));

// The same module genuinely appears in several repos. Keep the copy with the higher
// star count — that is the one whose page people actually landed on.
const byId = new Map();
for (const m of all) {
  const key = m.id.toLowerCase();
  const existing = byId.get(key);
  if (!existing || (m.stars || 0) > (existing.stars || 0)) byId.set(key, m);
}

const modules = [...byId.values()].sort((a, b) => (b.stars || 0) - (a.stars || 0));

if (modules.length < MIN_PLAUSIBLE) {
  process.stderr.write(
    `only ${modules.length} modules (< ${MIN_PLAUSIBLE}); refusing to overwrite\n`,
  );
  process.exit(1);
}

process.stderr.write(`built ${modules.length} modules\n`);
process.stdout.write(
  JSON.stringify({
    last_updated: new Date().toISOString(),
    total: modules.length,
    modules,
  }),
);
