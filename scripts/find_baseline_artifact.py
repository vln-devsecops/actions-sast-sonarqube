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
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def gh_api_get(url, token):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"GitHub API GET {url} failed: HTTP {e.code}\n{body}")


def select_artifact(artifacts, name):
    """Pick the artifact to use from a listing API response's `artifacts`
    array: exact name match, not expired, newest first."""
    candidates = [a for a in artifacts if a["name"] == name and not a["expired"]]
    if not candidates:
        return None
    # Exact-name matches for a content-addressed artifact name should never
    # collide across runs, but if they somehow do, prefer the newest.
    candidates.sort(key=lambda a: a["created_at"], reverse=True)
    return candidates[0]


def find_artifact(owner, repo, name, token):
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/actions/artifacts"
        f"?name={urllib.parse.quote(name)}&per_page=100"
    )
    data = gh_api_get(url, token)
    return select_artifact(data.get("artifacts", []), name)


def download_and_extract(artifact, token, dest_dir):
    req = urllib.request.Request(artifact["archive_download_url"])
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=120) as resp:
        blob = resp.read()
    os.makedirs(dest_dir, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(dest_dir)


def main():
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
