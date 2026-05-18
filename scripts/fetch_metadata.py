"""
fetch_metadata.py  (v4 — fdroid, gitlab, codeberg, flathub, winget, github, izzy)

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
# HTTP helpers
# ---------------------------------------------------------------------------

DATA_DIR = Path("data")

GITLAB_HDRS = {}
if tok := os.environ.get("GITLAB_TOKEN"):
    GITLAB_HDRS["PRIVATE-TOKEN"] = tok

CODEBERG_HDRS = {}
if tok := os.environ.get("CODEBERG_TOKEN"):
    CODEBERG_HDRS["Authorization"] = f"token {tok}"

GITHUB_HDRS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
if tok := os.environ.get("GH_PAT"):
    GITHUB_HDRS["Authorization"] = f"Bearer {tok}"

GITHUB_DELAY = 2.1   # GitHub search API: 30 req/min authenticated → 1 per 2s

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
        latest_vc = max(versions.keys(), key=lambda k: int(k) if k.isdigit() else -1, default=None) if versions else None
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
    page    = 1

    # Gitea search API: paginate through all repos sorted by stars.
    # Max 50 per page; no star-range filtering available (unlike GitHub).
    while len(entries) < TOP_N_PER_SOURCE:
        data = get(
            "https://codeberg.org/api/v1/repos/search",
            headers=CODEBERG_HDRS,
            params={
                "sort":  "stars",
                "order": "desc",
                "limit": 50,
                "page":  page,
            },
            pause=CODEBERG_DELAY,
        )
        if not data:
            break

        repos = data.get("data", [])
        if not repos:
            break

        for r in repos:
            full_name = r.get("full_name", "")
            if not full_name or full_name in seen:
                continue
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
                "stars":    r.get("stars_count", 0),
                "forks":    r.get("forks_count", 0),
                "language": r.get("language"),
                "topics":   r.get("topics", []),
                "homepage": r.get("html_url", f"https://codeberg.org/{full_name}"),
                "updated":  r.get("updated"),
                "version":  None,
                "detail_url": f"data/detail/codeberg/{owner}/{repo}.json",
            })

        if len(repos) < 50:
            break  # last page reached

        page += 1
        if page % 10 == 0:
            print(f"  page {page} → {len(entries):,} repos so far")

    entries.sort(key=lambda x: x["stars"], reverse=True)
    write_pages(DATA_DIR / "sources" / "codeberg", entries, "codeberg")
    print(f"[Codeberg] Done — {len(entries):,} repos")
    return entries


# ---------------------------------------------------------------------------
# GitHub — top Android repos by stars (uses GH_PAT secret)
# ---------------------------------------------------------------------------

def fetch_github() -> list:
    print("\n[GitHub] Discovering top Android repos …")
    entries = []
    seen    = set()

    # Multiple queries give broader coverage; each yields up to 1 000 results
    search_queries = [
        "topic:android stars:>100",
        "topic:android-app stars:>10",
        "topic:open-source-android",
    ]

    for query in search_queries:
        if len(entries) >= TOP_N_PER_SOURCE:
            break
        print(f"  Query: {query!r}")
        for page in range(1, 11):   # GitHub caps search at 1 000 results (10 pages × 100)
            if len(entries) >= TOP_N_PER_SOURCE:
                break
            data = get(
                "https://api.github.com/search/repositories",
                headers=GITHUB_HDRS,
                params={"q": query, "sort": "stars", "order": "desc", "per_page": 100, "page": page},
                pause=GITHUB_DELAY,
            )
            if not data:
                break
            items = data.get("items", [])
            if not items:
                break
            for repo in items:
                rid = repo["id"]
                if rid in seen:
                    continue
                seen.add(rid)
                owner = repo["owner"]["login"]
                rname = repo["name"]
                entries.append({
                    "id":         f"github:{rid}",
                    "source":     "github",
                    "name":       rname,
                    "summary":    (repo.get("description") or "")[:160],
                    "icon":       repo["owner"].get("avatar_url", ""),
                    "stars":      repo.get("stargazers_count", 0),
                    "homepage":   repo.get("html_url", f"https://github.com/{owner}/{rname}"),
                    "updated":    repo.get("updated_at"),
                    "version":    "",
                    "apk_url":    "",
                    "categories": repo.get("topics", []),
                    "license":    (repo.get("license") or {}).get("spdx_id", ""),
                    "language":   repo.get("language") or "",
                    "detail_url": f"data/detail/github/{owner}/{rname}.json",
                })
            if len(items) < 100:
                break   # last page for this query

    entries.sort(key=lambda x: x["stars"], reverse=True)
    write_pages(DATA_DIR / "sources" / "github", entries, "github")
    print(f"[GitHub] Done — {len(entries):,} repos")
    return entries


# ---------------------------------------------------------------------------
# IzzyOnDroid — full catalog from the IzzyOnDroid F-Droid repo index
# ---------------------------------------------------------------------------

def fetch_izzy() -> list:
    print("\n[IzzyOnDroid] Fetching from IzzyOnDroid F-Droid repository index …")
    raw = get("https://apt.izzysoft.de/fdroid/repo/index-v1.json")
    if not raw:
        print("[IzzyOnDroid] Index unavailable, skipping")
        return []

    apps     = raw.get("apps", [])
    packages = raw.get("packages", {})
    entries  = []

    for app in apps:
        pkg = app.get("packageName", "")
        if not pkg:
            continue

        # Latest release info
        pkg_versions = packages.get(pkg, [])
        latest   = pkg_versions[0] if pkg_versions else {}
        version  = latest.get("versionName", "")
        apk_name = latest.get("apkName", "")
        apk_url  = f"https://apt.izzysoft.de/fdroid/repo/{apk_name}" if apk_name else ""

        icon_file = app.get("icon") or ""
        icon_url  = f"https://apt.izzysoft.de/fdroid/repo/{icon_file}" if icon_file else ""

        # Prefer source code URL (usually GitHub); fall back to IzzyOnDroid page
        source_code = app.get("sourceCode") or app.get("webSite") or ""
        homepage    = source_code or f"https://apt.izzysoft.de/fdroid/index/apk/{pkg}"

        categories = app.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]

        added = app.get("added") or app.get("lastUpdated")

        entries.append({
            "id":         f"izzy:{pkg}",
            "source":     "izzy",
            "package":    pkg,
            "name":       app.get("name") or pkg,
            "summary":    (app.get("summary") or "")[:160],
            "icon":       icon_url,
            "stars":      0,
            "homepage":   homepage,
            "version":    version,
            "apk_url":    apk_url,
            "categories": categories,
            "license":    app.get("license") or "",
            "updated":    str(added) if added else "",
            "detail_url": f"data/detail/izzy/{slugify(pkg)}.json",
        })

    entries.sort(key=lambda x: x["name"].lower())
    write_pages(DATA_DIR / "sources" / "izzy", entries, "izzy")
    print(f"[IzzyOnDroid] Done — {len(entries):,} apps")
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

    # API can return:
    #   - a list of app dicts                         → use directly
    #   - a dict {"apps": [...]}                      → use raw["apps"]
    #   - a dict {appId: {...}, appId2: {...}}         → use dict values
    if isinstance(raw, list):
        apps = raw
    elif isinstance(raw, dict):
        if "apps" in raw:
            apps = raw["apps"]
        else:
            # Keys are app IDs, values are app objects
            apps = list(raw.values())
    else:
        print("[Flathub] Unexpected response type, skipping")
        return []

    print(f"  [Flathub] API returned {len(apps)} items")
    entries = []

    for app in apps:
        # Skip non-dict items
        if not isinstance(app, dict):
            continue

        # App ID can be in different fields depending on API version
        app_id = (
            app.get("id") or
            app.get("flatpakAppId") or
            app.get("appId") or
            app.get("app_id") or ""
        )
        if not app_id:
            continue

        name    = app.get("name") or app_id
        # name can be a dict {"en": "VLC"} in some API versions
        if isinstance(name, dict):
            name = name.get("en") or name.get("en-US") or app_id

        summary = app.get("summary") or app.get("description") or ""
        if isinstance(summary, dict):
            summary = summary.get("en") or summary.get("en-US") or ""
        summary = str(summary)[:160]

        icon = (
            app.get("icon") or
            app.get("iconUrl") or
            f"https://dl.flathub.org/repo/appstream/x86_64/icons/128x128/{app_id}.png"
        )

        releases = app.get("releases") or app.get("release") or []
        version  = None
        if releases and isinstance(releases, list) and isinstance(releases[0], dict):
            version = releases[0].get("version")

        categories = app.get("categories") or []
        if isinstance(categories, str):
            categories = [categories]

        entries.append({
            "id":         f"flathub:{app_id}",
            "source":     "flathub",
            "package":    app_id,
            "name":       str(name),
            "summary":    summary,
            "icon":       str(icon),
            "stars":      0,
            "categories": categories,
            "version":    version,
            "updated":    app.get("inStoreSinceDate") or app.get("addedAt"),
            "website":    (app.get("urls") or {}).get("homepage") if isinstance(app.get("urls"), dict) else None,
            "license":    app.get("projectLicense") or app.get("license"),
            "detail_url": f"data/detail/flathub/{slugify(app_id)}.json",
        })

    entries.sort(key=lambda x: x["name"].lower())
    write_pages(DATA_DIR / "sources" / "flathub", entries, "flathub")
    print(f"[Flathub] Done — {len(entries):,} apps")
    return entries


# ---------------------------------------------------------------------------
# Winget — packages from winget.run community API
# ---------------------------------------------------------------------------

def fetch_winget() -> list:
    print("\n[Winget] Fetching package catalog …")
    entries = []
    seen    = set()

    # Try winget.run API — supports cursor-based pagination
    # Endpoint returns {"Packages": [...], "ContinuationToken": "..."}
    continuation = None
    page_num = 0

    while True:
        params = {"MaximumResults": 100}
        if continuation:
            params["ContinuationToken"] = continuation

        data = get(
            "https://api.winget.run/v2/packages",
            params=params,
            pause=GENERIC_DELAY,
        )

        if not data:
            break

        # Handle different possible response shapes
        if isinstance(data, list):
            packages = data
            continuation = None
        elif isinstance(data, dict):
            # Try multiple common key names
            packages = (
                data.get("Packages") or
                data.get("packages") or
                data.get("data") or
                data.get("results") or
                []
            )
            continuation = data.get("ContinuationToken") or data.get("continuationToken")
        else:
            break

        if not packages:
            # Log what we actually got so the next fix is easier
            if isinstance(data, dict):
                print(f"  [Winget] Response keys: {list(data.keys())[:10]}")
            break

        for pkg in packages:
            if not isinstance(pkg, dict):
                continue
            pkg_id = (
                pkg.get("PackageIdentifier") or
                pkg.get("packageIdentifier") or
                pkg.get("id") or
                pkg.get("Id") or ""
            )
            if not pkg_id or pkg_id in seen:
                continue
            seen.add(pkg_id)

            name = (
                pkg.get("PackageName") or
                pkg.get("packageName") or
                pkg.get("name") or
                pkg.get("Name") or
                pkg_id
            )
            publisher = (
                pkg.get("Publisher") or
                pkg.get("publisher") or
                pkg.get("publisherName") or ""
            )
            versions = pkg.get("Versions") or pkg.get("versions") or []
            version  = None
            if versions and isinstance(versions, list):
                v = versions[0]
                version = v.get("PackageVersion") or v.get("packageVersion") or v.get("version") if isinstance(v, dict) else str(v)

            parts    = pkg_id.split(".", 1)
            pub_slug = slugify(parts[0]) if parts else "unknown"
            id_slug  = slugify(pkg_id)

            entries.append({
                "id":        f"winget:{pkg_id}",
                "source":    "winget",
                "package":   pkg_id,
                "name":      str(name),
                "publisher": str(publisher),
                "summary":   str(pkg.get("ShortDescription") or pkg.get("shortDescription") or pkg.get("description") or "")[:160],
                "icon":      pkg.get("IconUrl") or pkg.get("iconUrl") or "",
                "stars":     0,
                "version":   version,
                "updated":   None,
                "homepage":  pkg.get("PackageUrl") or pkg.get("homepage") or pkg.get("PublisherUrl") or "",
                "license":   pkg.get("License") or pkg.get("license") or "",
                "detail_url": f"data/detail/winget/{pub_slug}/{id_slug}.json",
            })

        page_num += 1
        if page_num % 50 == 0:
            print(f"  fetched {len(entries):,} winget packages so far …")

        # Stop if no continuation token and got a full page (means more pages exist)
        if not continuation:
            if not isinstance(data, list) or len(packages) < 100:
                break
        
        if len(entries) >= TOP_N_PER_SOURCE:
            break

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
            elif src in ("gitlab", "codeberg", "winget", "github", "izzy"):
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
                       key=lambda x: str(x.get("updated") or ""), reverse=True)

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
    print(" App metadata auto-discovery (fdroid/gitlab/codeberg/flathub/winget/github/izzy)")
    print("=" * 60)

    fd = fetch_fdroid()
    gl = fetch_gitlab()
    cb = fetch_codeberg()
    fh = fetch_flathub()
    wg = fetch_winget()
    gh = fetch_github()
    iz = fetch_izzy()

    all_apps = fd + gl + cb + fh + wg + gh + iz

    build_index(all_apps)
    write_meta({
        "fdroid":   len(fd),
        "gitlab":   len(gl),
        "codeberg": len(cb),
        "flathub":  len(fh),
        "winget":   len(wg),
        "github":   len(gh),
        "izzy":     len(iz),
    })
    prefetch_details(all_apps)

    print("\n" + "=" * 60)
    print(f" Finished — {len(all_apps):,} total apps")
    print("=" * 60)


if __name__ == "__main__":
    main()
