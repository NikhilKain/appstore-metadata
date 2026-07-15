#!/usr/bin/env python3
"""
build_fdroid_izzy.py  —  add-on for NikhilKain/appstore-metadata

Fetches the OFFICIAL F-Droid and IzzyOnDroid repo indexes (server-side, so no
browser CORS problems) and writes app records in the SAME shape your CDN
`data/index.json` already uses — the shape the Vyxel website's normCdn() reads:

    { "id": "fdroid:<pkg>", "source": "fdroid", "name", "summary",
      "icon", "stars", "homepage", "apkUrl", "version" }

Run it in your repo (GitHub Action or locally). It:
  1. downloads F-Droid  index-v2.json  and  IzzyOnDroid index-v2.json
  2. normalizes every app into the record shape above
  3. writes  data/fdroid.json  and  data/izzy.json
  4. merges them into  data/index.json  (replacing any old fdroid/izzy rows)

Then commit data/*.json — GitHub Pages serves them and the website's F-Droid /
IzzyOnDroid tiles fill from your fast CDN instead of dead CORS proxies.

Usage:   python scripts/build_fdroid_izzy.py
Deps:    only the Python standard library.
"""

import json, os, sys, urllib.request, gzip, io

REPOS = [
    # (source key, repo base url, [index urls to try in order])
    ("fdroid", "https://f-droid.org/repo", [
        "https://f-droid.org/repo/index-v2.json",
        "https://f-droid.org/repo/index-v1.json",
    ]),
    ("izzy", "https://apt.izzysoft.de/fdroid/repo", [
        "https://apt.izzysoft.de/fdroid/repo/index-v2.json",
        "https://apt.izzysoft.de/fdroid/repo/index-v1.json",
    ]),
]

# cap per source so the index stays lean; sorted by lastUpdated (newest first)
MAX_PER_SOURCE = 1500

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.normpath(os.path.join(HERE, "..", "data"))


def fetch_json(url):
    print(f"  ↓ {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "vyxel-metadata-builder"})
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    if raw[:2] == b"\x1f\x8b":  # gzip
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    return json.loads(raw.decode("utf-8"))


def en(val):
    """index-v2 localized strings are {"en-US": "..."} maps (or plain strings)."""
    if isinstance(val, dict):
        for k in ("en-US", "en", *val.keys()):
            if k in val:
                v = val[k]
                return v.get("name", "") if isinstance(v, dict) else v
        return ""
    return val or ""


def latest_version(pkg):
    versions = pkg.get("versions") or {}
    if not versions:
        return None, 0
    # versions is {hash: {added, manifest, file:{name}}}; pick highest 'added'
    best = max(versions.values(), key=lambda v: v.get("added", 0))
    return best, best.get("added", 0)


def normalize_v2(source, base, index):
    packages = index.get("packages") or {}
    rows = []
    for app_id, pkg in packages.items():
        meta = pkg.get("metadata") or {}
        name = en(meta.get("name")) or app_id
        summary = en(meta.get("summary")) or en(meta.get("description"))
        icon = ""
        ic = meta.get("icon")
        if isinstance(ic, dict):
            first = next(iter(ic.values()), None)
            if isinstance(first, dict):
                icon = base + first.get("name", "")
            elif isinstance(first, str):
                icon = base + first
        homepage = meta.get("webSite") or meta.get("sourceCode") or meta.get("issueTracker") or ""
        ver, added = latest_version(pkg)
        apk_url, vname = "", ""
        if ver:
            vname = str((ver.get("manifest") or {}).get("versionName", ""))
            fn = (ver.get("file") or {}).get("name", "")
            if fn:
                apk_url = base + fn
        rows.append(_row(source, app_id, name, summary, icon, homepage, apk_url, vname, added))
    return rows


def normalize_v1(source, base, index):
    apps = index.get("apps") or []
    pkgs = index.get("packages") or {}
    rows = []
    for app in apps:
        app_id = app.get("packageName") or ""
        loc = (app.get("localized") or {})
        el = loc.get("en-US") or loc.get("en") or (next(iter(loc.values()), {}) if loc else {})
        name = app.get("name") or el.get("name") or app_id
        summary = app.get("summary") or el.get("summary") or app.get("description") or el.get("description") or ""
        icon = ""
        if el.get("icon"):
            icon = f"{base}/{app_id}/en-US/{el['icon']}"
        elif app.get("icon"):
            icon = f"{base}/icons-640/{app['icon']}"
        homepage = app.get("webSite") or app.get("sourceCode") or app.get("issueTracker") or ""
        added = app.get("lastUpdated") or app.get("added") or 0
        apk_url, vname = "", ""
        plist = pkgs.get(app_id) or []
        if plist:
            best = max(plist, key=lambda v: v.get("added", 0))
            vname = str(best.get("versionName", ""))
            if best.get("apkName"):
                apk_url = f"{base}/{best['apkName']}"
        rows.append(_row(source, app_id, name, summary, icon, homepage, apk_url, vname, added))
    return rows


def _row(source, app_id, name, summary, icon, homepage, apk_url, vname, added):
    return {
        "id": f"{source}:{app_id}", "source": source, "name": name, "summary": summary,
        "icon": icon, "stars": 0, "homepage": homepage, "apkUrl": apk_url,
        "version": vname, "_added": added,
    }


def normalize(source, base, index):
    if isinstance(index.get("apps"), list):
        rows = normalize_v1(source, base, index)
    else:
        rows = normalize_v2(source, base, index)
    rows.sort(key=lambda r: r["_added"] or 0, reverse=True)
    rows = rows[:MAX_PER_SOURCE]
    for r in rows:
        r.pop("_added", None)
    print(f"  ✓ {source}: {len(rows)} apps")
    return rows


def main():
    os.makedirs(DATA, exist_ok=True)
    all_new = {}
    for source, base, urls in REPOS:
        rows = []
        for url in urls:
            try:
                idx = fetch_json(url)
                rows = normalize(source, base, idx)
                if rows:
                    break
            except Exception as e:
                print(f"  … {url} failed: {e}", file=sys.stderr)
        try:
            all_new[source] = rows
            with open(os.path.join(DATA, f"{source}.json"), "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"  ✗ {source} failed: {e}", file=sys.stderr)
            all_new[source] = []

    # merge into data/index.json
    index_path = os.path.join(DATA, "index.json")
    existing = []
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            loaded = json.load(f)
        existing = loaded if isinstance(loaded, list) else loaded.get("apps", [])

    kept = [a for a in existing if a.get("source") not in all_new]
    merged = kept
    for rows in all_new.values():
        merged = merged + rows

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=1)

    counts = {}
    for a in merged:
        counts[a.get("source")] = counts.get(a.get("source"), 0) + 1
    print(f"\n✔ wrote {index_path} — {len(merged)} apps total")
    print("  by source:", counts)


if __name__ == "__main__":
    main()
