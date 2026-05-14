"""
fetch_metadata.py  (v3 — fdroid, gitlab, codeberg, flathub, winget)

Automatically fetches the top apps from every configured source.
No manual lists needed.

Sources
-------
  F-Droid   — full catalog from official index-v2.json  (~5 000 packages)
  GitLab    — top projects by stars (topic: android)
  Codeberg  — top repos by stars    (Gitea API)
  Flathub   — full catalog from appstream API           (~2 500 apps)
  Winget    — full catalog from winget.run community API

Output layout
-------------
  data/
    meta.json                   overall stats (total, per-source counts, timestamp)
    index.json                  top TOP_INDEX_SIZE apps across all sources
    sources/
      fdroid/
        manifest.json           { total, pages, page_size }
        page-1.json             lightweight entries, PAGE_SIZE per page
        page-2.json
        ...
      gitlab/ codeberg/ flathub/ winget/   (same structure)
    detail/                     full detail files for top DETAIL_PREFETCH apps
      fdroid/<pkg>.json
      gitlab/<id>.json
      codeberg/<owner>/<repo>.json
      flathub/<pkg>.json
      winget/<publisher>/<id>.json
"""

import json
import os
import re
import time
from pathlib import Path
from datetime import datetime, timezone
import requests

# ---------------------------------------------------------------------------
# CONFIG — tune these if needed
# ---------------------------------------------------------------------------

TOP_N_PER_SOURCE  = 25_000   # target per source (GitLab, Codeberg)
TOP_INDEX_SIZE    = 5_000    # entries in index.json (what the app loads first)
DETAIL_PREFETCH   = 2_000    # write detail files eagerly for the top N apps
PAGE_SIZE         = 1_000    # apps per paginated source file

# ---------------------------------------------------------------------------
# Rate-limit delays (tune if you hit 429s)
# ---------------------------------------------------------------------------

GITLAB_DELAY    = 0.3   # seconds between GitLab API calls
CODEBERG_DELAY  = 0.5   # seconds between Codeberg API calls (lower limits than GitLab)
GENERIC_DELAY   = 0.1   # seconds between other API calls

# ---------------------------------------------------------------------------
# Codeberg star-range buckets
#
# Gitea's search API (used by Codeberg) caps results per query.
# Bucketing by star range lets us collect more unique results.
# ---------------------------------------------------------------------------

CODEBERG_STAR_RANGES = [
    ("10000", None),
    ("5000",  "9999"),
    ("2000",  "4999"),
    ("1000",  "1999"),
    ("500",   "999"),
    ("200",   "499"),
    ("100",   "199"),
    ("50",    "99"),
    ("20",    "49"),
    ("10",    "19"),
    ("5",     "9"),
    ("1",     "4"),
    ("0",     "0"),
]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

DATA_DIR = Path("data")

GITLAB_HDRS = {}
if tok := os.environ.get("GITLAB_TOKEN"):
    GITLAB_HDRS["PRIVATE-TOKEN"] = tok

CODEBERG_HDRS = {}
if tok := os.environ.get("CODEBERG_TOKEN"):
    CODEBERG_HDRS["Authorization"] = f"token {tok}"

_session = requests.Session()


def get(url, headers=None, params=None, retries=4, pause=0):
    for attempt in range(retries):
        try:
            r = _session.get(url, headers=headers or {}, params=params, timeout=30)
            if r.status_code in (429, 403):
                wait = int(r.headers.get("Retry-After", 60))
                print(f"      [rate-limit] sleeping {wait}s …")
                time.sleep(wait)
                continue
            r.raise_for_status()
            if pause:
                time.sleep(pause)
            return r.json()
        except requests.RequestException as exc:
            print(f"      [warn] {url} attempt {attempt + 1}: {exc}")
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None


def write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")))


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(text):
    return re.sub(r"[^a-z0-9\-_.]", "", str(text).lower().replace(" ", "-").replace("/", "-"))


def write_pages(base_dir: Path, items: list, source: str):
    """Splits items into PAGE_SIZE chunks and writes paginated JSON files."""
    base_dir.mkdir(parents=True, exist_ok=True)
    pages = [items[i : i + PAGE_SIZE] for i in range(0, max(len(items), 1), PAGE_SIZE)]
    write_json(base_dir / "manifest.json", {
        "source":    source,
        "total":     len(items),
        "pages":     len(pages),
        "page_size": PAGE_SIZE,
        "updated":   now_iso(),
    })
    for n, page in enumerate(pages, 1):
        write_json(base_dir / f"page-{n}.json", page)
    print(f"    wrote {len(items):,} entries → {base_dir} ({len(pages)} pages)")


# ---------------------------------------------------------------------------
# F-Droid — full catalog, sorted by lastUpdated
# ---------------------------------------------------------------------------

def fetch_fdroid() -> list:
    print("\n[F-Droid] Downloading full index …")
    raw = get("https://f-droid.org/repo/index-v2.json")
    if not raw:
        print("[F-Droid] Index unavailable, skipping")
        return []

    packages = raw.get("packages", {})
    entries  = []

    for pkg, info in packages.items():
        meta      = info.get("metadata", {})
        versions  = info.get("versions", {})
        latest_vc = max(versions.keys(), key=int, default=None) if versions else None
        latest    = versions.get(latest_vc, {}) if latest_vc else {}
        manifest  = latest.get("manifest", {})
        apk_file  = latest.get("file", {}).get("name", "")

        name     = (meta.get("name") or {}).get("en-US") or pkg
        summary  = ((meta.get("summary") or {}).get("en-US") or "")[:160]
        icon_obj = (meta.get("icon") or {}).get("en-US") or {}
        icon_url = (
            f"https://f-droid.org/repo/{pkg}/en-US/{icon_obj['name']}"
            if icon_obj.get("name") else ""
        )

        entries.append({
            "id":           f"fdroid:{pkg}",
            "source":       "fdroid",
            "package":      pkg,
            "name":         name,
            "summary":      summary,
            "icon":         icon_url,
            "stars":        0,
            "categories":   meta.get("categories", []),
            "version":      manifest.get("versionName"),
            "version_code": manifest.get("versionCode"),
            "apk_url":      f"https://f-droid.org/repo/{apk_file}" if apk_file else None,
            "updated":      meta.get("lastUpdated"),
            "source_code":  meta.get("sourceCode"),
            "website":      meta.get("webSite"),
            "detail_url":   f"data/detail/fdroid/{slugify(pkg)}.json",
        })

    # Most recently updated first = most actively maintained
    entries.sort(key=lambda x: x.get("updated") or "", reverse=True)
    write_pages(DATA_DIR / "sources" / "fdroid", entries, "fdroid")
    print(f"[F-Droid] Done — {len(entries):,} apps")
    return entries


# ---------------------------------------------------------------------------
# GitLab — top projects by stars
# ---------------------------------------------------------------------------

def fetch_gitlab() -> list:
    print("\n[GitLab] Discovering top projects …")
    entries = []
    seen    = set()
    page    = 1

    # Search across multiple topics to get more coverage
    search_topics = ["android", "linux", "flutter", "kotlin", "java"]

    for topic in search_topics:
        if len(entries) >= TOP_N_PER_SOURCE:
            break
        print(f"  Topic: {topic!r}")
        page = 1

        while len(entries) < TOP_N_PER_SOURCE:
            data = get(
                "https://gitlab.com/api/v4/projects",
                headers=GITLAB_HDRS,
                params={
                    "topic":    topic,
                    "order_by": "star_count",
                    "sort":     "desc",
                    "per_page": 100,
                    "page":     page,
                },
                pause=GITLAB_DELAY,
            )
            if not data or not isinstance(data, list) or len(data) == 0:
                break

            for p in data:
                pid = p["id"]
                if pid in seen:
                    continue
                seen.add(pid)
                entries.append({
                    "id":         f"gitlab:{pid}",
                    "source":     "gitlab",
                    "project_id": pid,
                    "name":       p.get("name", str(pid)),
                    "summary":    (p.get("description") or "")[:160],
                    "icon":       p.get("avatar_url") or "",
                    "stars":      p.get("star_count", 0),
                    "forks":      p.get("forks_count", 0),
                    "homepage":   p.get("web_url", ""),
                    "license":    (p.get("license") or {}).get("key"),
                    "language":   p.get("predominant_language"),
                    "topics":     p.get("topics", []),
                    "updated":    p.get("last_activity_at"),
                    "version":    None,
                    "detail_url": f"data/detail/gitlab/{pid}.json",
                })

            if len(data) < 100:
                break
            page += 1
            if page % 20 == 0:
                print(f"  page {page} → {len(entries):,} entries")

    entries.sort(key=lambda x: x["stars"], reverse=True)
    write_pages(DATA_DIR / "sources" / "gitlab", entries, "gitlab")
    print(f"[GitLab] Done — {len(entries):,} apps")
    return entries


# ---------------------------------------------------------------------------
# Codeberg — top repos by stars (Gitea API)
# ---------------------------------------------------------------------------

def fetch_codeberg() -> list:
    print("\n[Codeberg] Discovering top repos …")
    entries = []
    seen    = set()

    # Gitea search API supports sorting by stars.
    # We bucket by star count to bypass per-query result caps (similar to GitHub).
    for lo_str, hi_str in CODEBERG_STAR_RANGES:
        if len(entries) >= TOP_N_PER_SOURCE:
            break

        lo = int(lo_str)
        page = 1

        while len(entries) < TOP_N_PER_SOURCE:
            params = {
                "sort":  "stars",
                "order": "desc",
                "limit": 50,      # Codeberg/Gitea max is 50
                "page":  page,
            }
            # Gitea doesn't support star-range filtering in query params directly,
            # so we use the `q` param to search broadly and sort by stars.
            # After fetching, we stop when stars drop below the bucket floor.
            data = get(
                "https://codeberg.org/api/v1/repos/search",
                headers=CODEBERG_HDRS,
                params=params,
                pause=CODEBERG_DELAY,
            )
            if not data:
                break

            repos = data.get("data", [])
            if not repos:
                break

            added_this_page = 0
            for r in repos:
                full_name = r.get("full_name", "")
                if full_name in seen:
                    continue

                star_count = r.get("stars_count", 0)
                # Skip repos below the bucket floor to avoid duplicates
                if hi_str and star_count > int(hi_str):
                    continue
                if star_count < lo:
                    # Stars are sorted descending — once we're below the floor, stop
                    break

                seen.add(full_name)
                owner = r.get("owner", {}).get("login", "")
                repo  = r.get("name", "")
                entries.append({
                    "id":       f"codeberg:{full_name}",
                    "source":   "codeberg",
                    "owner":    owner,
                    "repo":     repo,
                    "name":     repo,
                    "summary":  (r.get("description") or "")[:160],
                    "icon":     r.get("avatar_url") or r.get("owner", {}).get("avatar_url", ""),
                    "stars":    star_count,
                    "forks":    r.get("forks_count", 0),
                    "language": r.get("language"),
                    "topics":   r.get("topics", []),
                    "homepage": r.get("html_url", f"https://codeberg.org/{full_name}"),
                    "updated":  r.get("updated",),
                    "version":  None,
                    "detail_url": f"data/detail/codeberg/{owner}/{repo}.json",
                })
                added_this_page += 1

            if len(repos) < 50 or added_this_page == 0:
                break
            page += 1

        print(f"  bucket ≥{lo_str} stars → {len(entries):,} total so far")

    entries.sort(key=lambda x: x["stars"], reverse=True)
    entries = entries[:TOP_N_PER_SOURCE]
    write_pages(DATA_DIR / "sources" / "codeberg", entries, "codeberg")
    print(f"[Codeberg] Done — {len(entries):,} repos")
    return entries


# ---------------------------------------------------------------------------
# Flathub — full catalog from appstream API
# ---------------------------------------------------------------------------

def fetch_flathub() -> list:
    print("\n[Flathub] Downloading full app catalog …")
    raw = get("https://flathub.org/api/v2/appstream")
    if not raw:
        print("[Flathub] Catalog unavailable, skipping")
        return []

    # The endpoint returns a list of app objects
    apps = raw if isinstance(raw, list) else raw.get("apps", [])
    entries = []

    for app in apps:
        app_id  = app.get("id") or app.get("flatpakAppId") or ""
        if not app_id:
            continue

        name    = app.get("name") or app_id
        summary = (app.get("summary") or "")[:160]

        # Icon: prefer remote URL, fall back to Flathub CDN pattern
        icon = app.get("icon") or f"https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/{app_id}.png"

        # Version: grab from latest release if present
        releases = app.get("releases") or app.get("release", []) or []
        version  = releases[0].get("version") if releases else None

        entries.append({
            "id":         f"flathub:{app_id}",
            "source":     "flathub",
            "package":    app_id,
            "name":       name,
            "summary":    summary,
            "icon":       icon,
            "stars":      0,
            "categories": app.get("categories", []),
            "version":    version,
            "updated":    app.get("inStoreSinceDate") or app.get("addedAt"),
            "website":    app.get("urls", {}).get("homepage") or app.get("projectLicense"),
            "license":    app.get("projectLicense"),
            "detail_url": f"data/detail/flathub/{slugify(app_id)}.json",
        })

    # Sort by name alphabetically (no star count for Flathub)
    entries.sort(key=lambda x: x["name"].lower())
    write_pages(DATA_DIR / "sources" / "flathub", entries, "flathub")
    print(f"[Flathub] Done — {len(entries):,} apps")
    return entries


# ---------------------------------------------------------------------------
# Winget — packages from winget.run community API
# ---------------------------------------------------------------------------

def fetch_winget() -> list:
    print("\n[Winget] Fetching package catalog from winget.run …")
    entries  = []
    seen     = set()
    skip     = 0
    per_page = 100

    while True:
        data = get(
            "https://api.winget.run/v2/packages",
            params={"limit": per_page, "skip": skip},
            pause=GENERIC_DELAY,
        )
        if not data:
            break

        # winget.run returns either a list or {"packages": [...], "count": N}
        if isinstance(data, list):
            packages = data
        elif isinstance(data, dict):
            packages = data.get("packages") or data.get("data") or []
        else:
            break

        if not packages:
            break

        for pkg in packages:
            pkg_id = pkg.get("id") or pkg.get("packageIdentifier") or ""
            if not pkg_id or pkg_id in seen:
                continue
            seen.add(pkg_id)

            name      = pkg.get("name") or pkg.get("packageName") or pkg_id
            publisher = pkg.get("publisher") or pkg.get("publisherName") or ""
            versions  = pkg.get("versions") or []
            version   = versions[0].get("packageVersion") if versions else pkg.get("version")

            # Slugify the id for the detail file path
            parts = pkg_id.split(".", 1)
            pub_slug = slugify(parts[0]) if parts else "unknown"
            id_slug  = slugify(pkg_id)

            entries.append({
                "id":        f"winget:{pkg_id}",
                "source":    "winget",
                "package":   pkg_id,
                "name":      name,
                "publisher": publisher,
                "summary":   (pkg.get("description") or pkg.get("shortDescription") or "")[:160],
                "icon":      pkg.get("iconUrl") or "",
                "stars":     0,
                "version":   version,
                "updated":   None,
                "homepage":  pkg.get("homepage") or pkg.get("publisherUrl") or "",
                "license":   pkg.get("license") or "",
                "detail_url": f"data/detail/winget/{pub_slug}/{id_slug}.json",
            })

        skip += per_page
        if len(packages) < per_page:
            break   # last page

        if len(entries) % 5000 == 0 and len(entries) > 0:
            print(f"  fetched {len(entries):,} winget packages so far …")

    # Sort alphabetically by name
    entries.sort(key=lambda x: x["name"].lower())
    write_pages(DATA_DIR / "sources" / "winget", entries, "winget")
    print(f"[Winget] Done — {len(entries):,} packages")
    return entries


# ---------------------------------------------------------------------------
# Detail files — eagerly written for the top DETAIL_PREFETCH apps
# ---------------------------------------------------------------------------

def _write_fdroid_detail(entry: dict):
    """F-Droid detail: fetch full description from the API."""
    pkg  = entry["package"]
    data = get(f"https://f-droid.org/api/v1/packages/{pkg}", pause=GENERIC_DELAY)
    if not data:
        # Fall back to what we already have in the index entry
        write_json(DATA_DIR / "detail" / "fdroid" / f"{slugify(pkg)}.json",
                   {**entry, "fetched_at": now_iso()})
        return
    write_json(DATA_DIR / "detail" / "fdroid" / f"{slugify(pkg)}.json", {
        **entry,
        "description": (data.get("description") or "")[:5000],
        "fetched_at":  now_iso(),
    })


def _write_flathub_detail(entry: dict):
    """Flathub detail: fetch per-app info from the appstream endpoint."""
    pkg  = entry["package"]
    data = get(f"https://flathub.org/api/v2/appstream/{pkg}", pause=GENERIC_DELAY)
    if not data:
        write_json(DATA_DIR / "detail" / "flathub" / f"{slugify(pkg)}.json",
                   {**entry, "fetched_at": now_iso()})
        return
    write_json(DATA_DIR / "detail" / "flathub" / f"{slugify(pkg)}.json", {
        **entry,
        "description":  (data.get("description") or "")[:5000],
        "screenshots":  data.get("screenshots", [])[:5],
        "fetched_at":   now_iso(),
    })


def prefetch_details(all_apps: list):
    # For sources with stars, rank by stars; for others, keep as-is
    ranked     = sorted(all_apps, key=lambda x: x.get("stars", 0), reverse=True)
    candidates = ranked[:DETAIL_PREFETCH]
    done       = 0

    print(f"\n[Details] Pre-fetching for top {len(candidates):,} apps …")
    for i, entry in enumerate(candidates):
        dest = DATA_DIR / entry["detail_url"].removeprefix("data/")
        if dest.exists():
            age_h = (time.time() - dest.stat().st_mtime) / 3600
            if age_h < 23:
                continue

        try:
            src = entry["source"]
            if src == "fdroid":
                _write_fdroid_detail(entry)
                done += 1
            elif src == "flathub":
                _write_flathub_detail(entry)
                done += 1
            # GitLab, Codeberg, and Winget already have enough data
            # in the index entry to render a full detail page without
            # an extra API call — so we just copy the index entry.
            elif src in ("gitlab", "codeberg", "winget"):
                dest.parent.mkdir(parents=True, exist_ok=True)
                write_json(dest, {**entry, "fetched_at": now_iso()})
                done += 1
        except Exception as exc:
            print(f"    [warn] {entry['id']}: {exc}")

        if i % 500 == 0 and i > 0:
            print(f"  {i:,} / {len(candidates):,} processed …")

    print(f"[Details] Done — {done} new detail files written")


# ---------------------------------------------------------------------------
# Master index  (top TOP_INDEX_SIZE apps, stars-first then updated-date)
# ---------------------------------------------------------------------------

def build_index(all_apps: list):
    starred   = sorted([a for a in all_apps if a.get("stars", 0) > 0],
                       key=lambda x: x["stars"], reverse=True)
    unstarred = sorted([a for a in all_apps if a.get("stars", 0) == 0],
                       key=lambda x: x.get("updated") or "", reverse=True)

    top = (starred + unstarred)[:TOP_INDEX_SIZE]
    write_json(DATA_DIR / "index.json", {
        "last_updated": now_iso(),
        "total_all":    len(all_apps),
        "total_index":  len(top),
        "apps":         top,
    })
    print(f"\n[Index] index.json → {len(top):,} apps (of {len(all_apps):,} total)")


def write_meta(counts: dict):
    total = sum(counts.values())
    write_json(DATA_DIR / "meta.json", {
        "last_updated": now_iso(),
        "total":        total,
        "sources":      counts,
    })
    print(f"[Meta]  meta.json → {total:,} apps across {len(counts)} sources")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("=" * 60)
    print(" App metadata auto-discovery (fdroid/gitlab/codeberg/flathub/winget)")
    print("=" * 60)

    fd = fetch_fdroid()
    gl = fetch_gitlab()
    cb = fetch_codeberg()
    fh = fetch_flathub()
    wg = fetch_winget()

    all_apps = fd + gl + cb + fh + wg

    build_index(all_apps)
    write_meta({
        "fdroid":   len(fd),
        "gitlab":   len(gl),
        "codeberg": len(cb),
        "flathub":  len(fh),
        "winget":   len(wg),
    })
    prefetch_details(all_apps)

    print("\n" + "=" * 60)
    print(f" Finished — {len(all_apps):,} total apps")
    print("=" * 60)


if __name__ == "__main__":
    main()
