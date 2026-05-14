"""
refresh_index.py — quick hourly refresh

Runs in < 5 minutes. Does NOT re-discover apps from source APIs.
Instead it:
  1. Re-reads already-committed source page files
  2. Re-builds index.json from them
  3. Refreshes detail files for any top-2000 app that hasn't been updated in 23+ hours
  4. Updates meta.json timestamp

Use this for the hourly cron job.
Run fetch_metadata.py (the full script) for daily re-discovery.
"""

import json
from pathlib import Path
from datetime import datetime, timezone
import sys

sys.path.insert(0, str(Path(__file__).parent))
from fetch_metadata import (
    DATA_DIR, TOP_INDEX_SIZE, DETAIL_PREFETCH, now_iso,
    write_json, write_meta, build_index, prefetch_details,
)

SOURCES = ["fdroid", "gitlab", "codeberg", "flathub", "winget"]


def load_all_apps() -> list:
    """Reads all committed page files back into memory."""
    all_apps = []
    for src in SOURCES:
        manifest_path = DATA_DIR / "sources" / src / "manifest.json"
        if not manifest_path.exists():
            print(f"  [skip] no manifest for {src}")
            continue
        manifest = json.loads(manifest_path.read_text())
        for n in range(1, manifest["pages"] + 1):
            page_path = DATA_DIR / "sources" / src / f"page-{n}.json"
            if page_path.exists():
                all_apps.extend(json.loads(page_path.read_text()))
    return all_apps


def main():
    print("=" * 60)
    print(" Quick index refresh")
    print("=" * 60)

    all_apps = load_all_apps()
    if not all_apps:
        print("No cached source files found. Run fetch_metadata.py first.")
        return

    print(f"Loaded {len(all_apps):,} apps from cached pages")

    build_index(all_apps)

    counts = {}
    for src in SOURCES:
        manifest_path = DATA_DIR / "sources" / src / "manifest.json"
        if manifest_path.exists():
            m = json.loads(manifest_path.read_text())
            counts[src] = m.get("total", 0)
        else:
            counts[src] = 0
    write_meta(counts)

    prefetch_details(all_apps)

    print("\nQuick refresh done")


if __name__ == "__main__":
    main()
