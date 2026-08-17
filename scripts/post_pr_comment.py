#!/usr/bin/env python3
"""Post (or update) a human-readable PR summary comment from apply_policy.py's
result JSON. This is in addition to the Check Run annotations apply_policy.py
creates, not a replacement: annotations are inline-on-diff, this comment is
the at-a-glance summary.

Matches on a hidden HTML-comment marker in the comment body (the same
pattern used by most PR-bot actions) so repeated pushes to the same PR update
the existing comment instead of piling up a new one each time.
"""
import argparse
import json
import os
import urllib.error
import urllib.request

MARKER = "<!-- sonarqube-community-action:pr-summary -->"


def gh_api(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise SystemExit(f"GitHub API {method} {url} failed: HTTP {e.code}\n{body_text}")


def find_existing_comment(owner, repo, pr_number, token):
    page = 1
    while True:
        url = (
            f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
            f"?per_page=100&page={page}"
        )
        comments = gh_api("GET", url, token)
        for comment in comments:
            if MARKER in comment.get("body", ""):
                return comment["id"]
        if len(comments) < 100:
            return None
        page += 1


def render_body(result, baseline_tier):
    counts = result["by_severity"]
    order = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]

    lines = ["## SonarQube Community scan", ""]
    if result["blocked"]:
        lines.append(f"**Blocked** - a new finding meets or exceeds the `{result['threshold']}` threshold.")
    elif result["total_new_findings"]:
        lines.append("Not blocked - no new finding meets the blocking severity threshold.")
    else:
        lines.append("No new findings introduced by this PR. :white_check_mark:")
    lines += ["", "| Severity | New findings |", "| --- | --- |"]
    lines += [f"| {sev} | {counts.get(sev, 0)} |" for sev in order]
    lines += ["", f"Baseline: **{baseline_tier}**  •  Blocking policy: **{'enabled' if result['blocking_enabled'] else 'disabled'}** (threshold `{result['threshold']}`)"]
    if result.get("check_run_url"):
        lines += ["", f"[View annotated findings]({result['check_run_url']})"]
    lines += ["", MARKER]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--result", required=True, help="Path to apply_policy.py's result JSON")
    parser.add_argument("--baseline-tier", required=True, help="Human-readable description of which baseline tier was used")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required")

    owner, repo = args.repo.split("/", 1)
    with open(args.result) as f:
        result = json.load(f)

    body = render_body(result, args.baseline_tier)

    existing_id = find_existing_comment(owner, repo, args.pr_number, token)
    if existing_id:
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/comments/{existing_id}"
        gh_api("PATCH", url, token, {"body": body})
        print(f"Updated PR comment {existing_id}")
    else:
        url = f"https://api.github.com/repos/{owner}/{repo}/issues/{args.pr_number}/comments"
        comment = gh_api("POST", url, token, {"body": body})
        print(f"Created PR comment {comment['id']}")


if __name__ == "__main__":
    main()
