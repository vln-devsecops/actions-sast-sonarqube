#!/usr/bin/env python3
"""Evaluate the blocking policy against a set of new findings, publish them as
a GitHub Check Run (one annotation per finding), and exit non-zero if the
policy trips and blocking is enabled.

The policy is our own, evaluated purely against the new-findings set produced
by diff_findings.py - not SonarQube's Quality Gate, which assumes persisted
server-side baseline/period state this action deliberately doesn't keep.

Always writes --result-out, even when it goes on to exit non-zero, so a
subsequent workflow step (e.g. posting a PR summary comment) can read the
outcome with `if: always()`.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

# Legacy severity scale, low to high. Kept as the policy-relevant field
# because SonarQube populates it for backward compatibility regardless of
# whether the instance runs in Standard or MQR analysis mode; impacts[] (the
# newer per-software-quality model) is carried along in the findings schema
# for context but isn't what the default policy evaluates. MAJOR is the
# positional equivalent of impacts[].severity: MEDIUM on the newer 5-level
# scale, if that mapping matters later.
SEVERITY_ORDER = ["INFO", "MINOR", "MAJOR", "CRITICAL", "BLOCKER"]

ANNOTATION_LEVEL = {
    "BLOCKER": "failure",
    "CRITICAL": "failure",
    "MAJOR": "warning",
    "MINOR": "notice",
    "INFO": "notice",
}

ANNOTATION_BATCH_SIZE = 50

# fetch_findings.py guarantees every severity it emits is in SEVERITY_ORDER,
# but baseline artifacts are persisted JSON that can be up to
# artifact-retention-days old - a baseline written before that guarantee
# existed can still carry a null/unknown severity. Fail closed on anything
# unrecognized rather than crashing (ValueError/KeyError) or, worse, dropping
# the finding silently out of the reported counts.
UNKNOWN_SEVERITY_FALLBACK = "MAJOR"


def severity_of(finding):
    severity = finding.get("severity")
    return severity if severity in SEVERITY_ORDER else UNKNOWN_SEVERITY_FALLBACK


def severity_at_least(severity, threshold):
    if severity not in SEVERITY_ORDER:
        severity = UNKNOWN_SEVERITY_FALLBACK
    return SEVERITY_ORDER.index(severity) >= SEVERITY_ORDER.index(threshold)


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


def to_annotation(finding):
    line = finding.get("line") or 1
    severity = severity_of(finding)
    title = f"{finding['rule']} ({severity})"
    if finding.get("type") == "SECURITY_HOTSPOT":
        title = f"Security Hotspot: {title}"
    return {
        "path": finding["path"],
        "start_line": line,
        "end_line": line,
        "annotation_level": ANNOTATION_LEVEL[severity],
        "title": title,
        "message": finding.get("message", ""),
    }


def counts_by_severity(findings):
    counts = {s: 0 for s in SEVERITY_ORDER}
    for f in findings:
        counts[severity_of(f)] += 1
    return counts


def build_summary(findings, blocking_findings, threshold, blocking_enabled):
    counts = counts_by_severity(findings)
    lines = [
        f"Found {len(findings)} new finding(s) not present in the baseline.",
        "",
        "| Severity | Count |",
        "| --- | --- |",
    ]
    for severity in reversed(SEVERITY_ORDER):
        lines.append(f"| {severity} | {counts[severity]} |")
    lines.append("")
    if blocking_enabled:
        lines.append(
            f"Policy threshold: **{threshold}** or above blocks the job. "
            f"{len(blocking_findings)} new finding(s) meet or exceed it."
        )
    else:
        lines.append("Blocking is disabled for this run; findings are reported only.")
    return "\n".join(lines)


def batch(items, size):
    """Split items into consecutive chunks of at most `size`. GitHub's Check
    Run API only accepts ANNOTATION_BATCH_SIZE annotations per request, so
    the first batch goes in the creating POST and the rest follow as PATCHes."""
    return [items[i : i + size] for i in range(0, len(items), size)]


def create_check_run(repo, token, sha, check_name, conclusion, summary, annotations):  # pragma: no cover - network I/O, validated live
    owner, name = repo.split("/", 1)
    base_url = f"https://api.github.com/repos/{owner}/{name}/check-runs"

    batches = batch(annotations, ANNOTATION_BATCH_SIZE) or [[]]
    check_run = gh_api(
        "POST",
        base_url,
        token,
        {
            "name": check_name,
            "head_sha": sha,
            "status": "completed",
            "conclusion": conclusion,
            "output": {
                "title": check_name,
                "summary": summary,
                "annotations": batches[0],
            },
        },
    )

    check_run_id = check_run["id"]
    update_url = f"{base_url}/{check_run_id}"
    for remaining_batch in batches[1:]:
        gh_api(
            "PATCH",
            update_url,
            token,
            {"output": {"title": check_name, "summary": summary, "annotations": remaining_batch}},
        )

    return check_run


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--findings", required=True, help="Path to new-findings JSON (diff_findings.py output)")
    parser.add_argument("--threshold", default="MAJOR", choices=SEVERITY_ORDER)
    parser.add_argument("--blocking", default="true", choices=["true", "false"])
    parser.add_argument("--repo", required=True, help="owner/repo")
    parser.add_argument("--sha", required=True, help="commit SHA the check run attaches to (PR head SHA)")
    parser.add_argument("--check-name", default="sonarqube-community/new-findings")
    parser.add_argument("--result-out", required=True, help="Path to write the policy result JSON to")
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("GITHUB_TOKEN environment variable is required")

    blocking_enabled = args.blocking == "true"

    with open(args.findings) as f:
        findings = json.load(f)

    blocking_findings = [f for f in findings if severity_at_least(severity_of(f), args.threshold)]
    blocked = blocking_enabled and bool(blocking_findings)

    if blocked:
        conclusion = "failure"
    elif findings:
        conclusion = "neutral"
    else:
        conclusion = "success"

    summary = build_summary(findings, blocking_findings, args.threshold, blocking_enabled)
    annotations = [to_annotation(f) for f in findings]

    check_run = create_check_run(
        args.repo, token, args.sha, args.check_name, conclusion, summary, annotations
    )

    result = {
        "threshold": args.threshold,
        "blocking_enabled": blocking_enabled,
        "total_new_findings": len(findings),
        "blocking_findings": len(blocking_findings),
        "by_severity": counts_by_severity(findings),
        "blocked": blocked,
        "check_run_url": check_run.get("html_url"),
    }
    with open(args.result_out, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")

    print(summary)

    if blocked:
        sys.exit(1)


if __name__ == "__main__":
    main()
