#!/usr/bin/env python3
"""
Pre-fetches GitHub release data for repos listed in github_repos.json.
Writes results to data/releases/github/{owner}/{repo}.json so the
Android app can load release info without a user-supplied GitHub token.

Output: data/releases/github/{owner}/{repo}.json
  -> JSON array of up to RELEASES_PER_REPO release objects, newest first.
     Shape matches GitHub's GET /repos/{owner}/{repo}/releases response.

Add repos to github_repos.json at the root of this repository.
"""

import json
import os
import sys
import time

# Windows consoles default to cp1252, which can't print …/→ in log lines.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass
from pathlib import Path
import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPOS_FILE        = Path(__file__).parent.parent / "github_repos.json"
OUTPUT_DIR        = Path("data") / "releases" / "github"
GITHUB_TOKEN      = os.environ.get("GH_PAT", "")
API_BASE          = "https://api.github.com"
RELEASES_PER_REPO = 10      # releases to store per repo (newest first)
SLEEP_BETWEEN     = 0.25    # seconds between requests

# ---------------------------------------------------------------------------
# HTTP helpers  (reuse requests.Session for connection pooling)
# ---------------------------------------------------------------------------

_session = requests.Session()
_session.headers.update({
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
})
if GITHUB_TOKEN:
    _session.headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"


def get(url: str, params: dict = None):
    """GET with retry and rate-limit handling. Returns parsed JSON or None."""
    for attempt in range(4):
        try:
            r = _session.get(url, params=params, timeout=20)
            if r.status_code == 404:
                return None          # no releases or repo not found — skip silently
            if r.status_code == 401 and "Authorization" in _session.headers:
                # Expired/revoked GH_PAT: a bad token is worse than none —
                # drop it for the whole session and retry unauthenticated.
                print("  [auth] GH_PAT rejected (401) — falling back to unauthenticated requests. "
                      "Regenerate the GH_PAT repository secret!", flush=True)
                del _session.headers["Authorization"]
                continue
            if r.status_code in (403, 429):
                wait = int(r.headers.get("Retry-After", 60))
                print(f"  [rate-limit] sleeping {wait}s …", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt < 3:
                time.sleep(2 ** attempt)
            else:
                print(f"  [error] {url}: {exc}")
    return None


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if not REPOS_FILE.exists():
        print(f"ERROR: {REPOS_FILE} not found — create github_repos.json at repo root.")
        return

    repos = json.loads(REPOS_FILE.read_text(encoding="utf-8"))
    print(f"Fetching releases for {len(repos)} repos …")
    if not GITHUB_TOKEN:
        print("WARNING: GH_PAT not set — unauthenticated (60 req/hr limit).")

    ok = 0
    skipped = 0

    for i, entry in enumerate(repos, 1):
        entry = entry.strip()
        if "/" not in entry:
            print(f"[{i}/{len(repos)}] Skipping malformed entry: {entry!r}")
            continue

        owner, repo = entry.split("/", 1)
        out_file    = OUTPUT_DIR / owner / f"{repo}.json"

        print(f"[{i}/{len(repos)}] {owner}/{repo} … ", end="", flush=True)

        releases = get(
            f"{API_BASE}/repos/{owner}/{repo}/releases",
            params={"per_page": RELEASES_PER_REPO},
        )

        if not releases:
            print("no releases or not found, skipping")
            skipped += 1
        else:
            write_json(out_file, releases)
            label = "release" if len(releases) == 1 else "releases"
            print(f"OK ({len(releases)} {label})")
            ok += 1

        time.sleep(SLEEP_BETWEEN)

    print(f"\nDone — {ok} written, {skipped} skipped.")


if __name__ == "__main__":
    main()
