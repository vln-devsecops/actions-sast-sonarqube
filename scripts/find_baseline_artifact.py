#!/usr/bin/env python3
"""Look up a workflow artifact by exact name across ALL workflow runs in a
repo (not just the current run), and download + extract it if found.

`actions/download-artifact` alone can only see artifacts produced earlier in
the *same* run, or (with `run-id:`) a run you already know the ID of. Finding
"whichever baseline artifact happens to be named sonar-baseline-<sha>" needs
a name-filtered listing across the whole repo, which only the REST API
provides: GET /repos/{owner}/{repo}/actions/artifacts?name=...

Prints "true" or "false" to stdout (nothing else) so a workflow step can do:
  found=$(find_baseline_artifact.py --repo ... --name ... --dest ...)
"""
import argparse
import io
import os
import sys
import urllib.parse
import zipfile

from gh_client import request as gh_request


def gh_api_get(url, token):  # pragma: no cover - network I/O, validated live
    return gh_request("GET", url, token)


ARTIFACTS_PER_PAGE = 100
# Guard against an unbounded loop if the API ever returns a full page
# forever; 20 pages x 100 is far beyond any plausible number of artifacts
# sharing one content-addressed name.
MAX_ARTIFACT_PAGES = 20


def select_artifact(artifacts, name):
    """Pick the artifact to use from a listing API response's `artifacts`
    array: exact name match, not expired, newest first."""
    candidates = [
        a for a in artifacts
        if a.get("name") == name and not a.get("expired", False)
    ]
    if not candidates:
        return None
    # Exact-name matches for a content-addressed artifact name should never
    # collide across runs, but if they somehow do, prefer the newest.
    # created_at is ISO 8601 UTC, so a lexicographic sort is chronological.
    candidates.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return candidates[0]


def find_artifact(owner, repo, name, token):  # pragma: no cover - network I/O, validated live
    """Search every page of the name-filtered listing, not just the first.
    A valid-but-unseen baseline is indistinguishable from no baseline at the
    call site, so an incomplete search silently changes which baseline the
    PR diff is computed against."""
    artifacts = []
    for page in range(1, MAX_ARTIFACT_PAGES + 1):
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/actions/artifacts"
            f"?name={urllib.parse.quote(name)}"
            f"&per_page={ARTIFACTS_PER_PAGE}&page={page}"
        )
        page_artifacts = gh_api_get(url, token).get("artifacts", [])
        artifacts.extend(page_artifacts)
        if len(page_artifacts) < ARTIFACTS_PER_PAGE:
            break
    return select_artifact(artifacts, name)


def download_and_extract(artifact, token, dest_dir):  # pragma: no cover - network I/O, validated live
    blob = gh_request("GET", artifact["archive_download_url"], token, timeout=120, raw=True)
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        # This extractall() is NOT a "Zip Slip" path-traversal hole, despite
        # matching the pattern that usually is one - it has been raised in
        # review before, so: CPython sanitizes every member name during
        # extraction. Per the zipfile docs for extract(), an absolute member
        # path has its drive/UNC prefix and leading separators stripped, and
        # ALL ".." components are removed - so a member named
        # "../../../../etc/passwd" lands at "etc/passwd" *underneath*
        # dest_dir and cannot escape it. Verified empirically against a
        # hand-built malicious archive, not assumed from the docs alone.
        #
        # Note this is a property of extractall()/extract() specifically. If
        # this is ever rewritten as a per-member loop (e.g. to filter which
        # members get written), that rewrite has to re-implement the same
        # sanitization by hand - open(os.path.join(dest_dir, name), "wb") on
        # a raw member name genuinely is exploitable.
        zf.extractall(dest_dir)


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--name", required=True, help="exact artifact name to look up")
    parser.add_argument("--dest", required=True, help="directory to extract the artifact into")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required")

    owner, repo = args.repo.split("/", 1)
    artifact = find_artifact(owner, repo, args.name, token)

    if artifact is None:
        print(f"No non-expired artifact named '{args.name}' found in {args.repo}.", file=sys.stderr)
        print("false")
        return

    download_and_extract(artifact, token, args.dest)
    print(
        f"Downloaded artifact '{args.name}' (id={artifact['id']}, created_at={artifact['created_at']}) "
        f"to {args.dest}",
        file=sys.stderr,
    )
    print("true")


if __name__ == "__main__":
    main()
