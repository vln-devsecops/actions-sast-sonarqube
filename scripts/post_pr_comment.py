#!/usr/bin/env python3
"""Post (or update) a human-readable PR summary comment from apply_policy.py's
result JSON, plus the baseline and resolved findings diff_findings.py
compared against - shown as one severity-by-severity table (Baseline /
Added / Removed), so a reviewer can tell "0 new findings" against an
already-clean baseline apart from "0 new findings" against one carrying a
pile of known defects, and see at a glance whether this PR is trending the
defect count up or down. This is in addition to the Check Run annotations
apply_policy.py creates, not a replacement: annotations are inline-on-diff,
this comment is the at-a-glance summary.

Matches on a hidden HTML-comment marker in the comment body (the same
pattern used by most PR-bot actions) so repeated pushes to the same PR update
the existing comment instead of piling up a new one each time.
"""
import argparse
import json
import os
import urllib.error
import urllib.request

from apply_policy import counts_by_severity

MARKER = "<!-- sonarqube-community-action:pr-summary -->"


def gh_api(method, url, token, body=None):  # pragma: no cover - network I/O, validated live
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


def find_existing_comment(owner, repo, pr_number, token):  # pragma: no cover - network I/O, validated live
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


def render_body(result, baseline_tier, baseline_counts, removed_counts):
    added_counts = result["by_severity"]
    order = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]

    lines = ["## SAST SonarQube scan", ""]
    if result["blocked"]:
        lines.append(f"**Blocked** - a new finding meets or exceeds the `{result['threshold']}` threshold.")
    elif result["total_new_findings"]:
        lines.append("Not blocked - no new finding meets the blocking severity threshold.")
    else:
        lines.append("No new findings introduced by this PR. :white_check_mark:")

    # One table, not separate new-findings/baseline tables: Added alone
    # can't tell a reviewer whether "0 new findings" means a clean baseline
    # or one already carrying a pile of known defects, and a severity-level
    # Baseline/Added/Removed row is what actually shows whether a PR is
    # trending the defect count up or down.
    lines += ["", "| Severity | Baseline | Added | Removed |", "| --- | --- | --- | --- |"]
    lines += [
        f"| {sev} | {baseline_counts.get(sev, 0)} | {added_counts.get(sev, 0)} | {removed_counts.get(sev, 0)} |"
        for sev in order
    ]

    lines += ["", f"Baseline: **{baseline_tier}**  •  Blocking policy: **{'enabled' if result['blocking_enabled'] else 'disabled'}** (threshold `{result['threshold']}`)"]
    if result.get("check_run_url"):
        lines += ["", f"[View annotated findings]({result['check_run_url']})"]
    lines += ["", MARKER]
    return "\n".join(lines)


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--pr-number", required=True, type=int)
    parser.add_argument("--result", required=True, help="Path to apply_policy.py's result JSON")
    parser.add_argument("--baseline-findings", required=True, help="Path to the baseline findings JSON diff_findings.py compared against")
    parser.add_argument("--removed-findings", required=True, help="Path to diff_findings.py's --removed-out JSON (findings resolved by this PR)")
    parser.add_argument("--baseline-tier", required=True, help="Human-readable description of which baseline tier was used")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required")

    owner, repo = args.repo.split("/", 1)
    with open(args.result) as f:
        result = json.load(f)
    with open(args.baseline_findings) as f:
        baseline_findings = json.load(f)
    with open(args.removed_findings) as f:
        removed_findings = json.load(f)

    body = render_body(
        result, args.baseline_tier,
        counts_by_severity(baseline_findings), counts_by_severity(removed_findings),
    )

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
