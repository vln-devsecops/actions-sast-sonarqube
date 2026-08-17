#!/usr/bin/env python3
"""Set-diff a head findings file against a baseline findings file, producing
the findings present in head but not in baseline ("new findings").

Matching rule (see README for rationale): two findings are the same issue if
they share (ruleKey, componentPath, hash) - or, when a finding's `hash` is
empty (SonarQube returns no hash for some issue types, e.g. cross-file
duplication, and never returns one for Security Hotspots), if they share
(ruleKey, componentPath, line) instead. `hash` is
a checksum of the source line's content at issue-creation time, so it
survives unrelated line-number churn elsewhere in the file; we're applying it
across two independent analyses instead of across one analysis's history, the
way SonarQube's own issue tracker does internally.
"""
import argparse
import json


def finding_key(finding):
    if finding.get("hash"):
        return ("hash", finding["rule"], finding["path"], finding["hash"])
    return ("line", finding["rule"], finding["path"], finding.get("line"))


def diff(baseline, head):
    """Findings in `head` not present in `baseline`. Symmetric in the sense
    that swapping the arguments (diff(head, baseline)) gives the opposite
    direction - findings in `baseline` no longer present in `head`, i.e.
    resolved findings - which main() uses for --removed-out below rather
    than duplicating this logic."""
    baseline_keys = {finding_key(f) for f in baseline}
    return [f for f in head if finding_key(f) not in baseline_keys]


def filter_by_changed_files(findings, changed_files):
    """Keep only findings whose `path` is in `changed_files`. Both must be in
    the same path-space as each other - the caller (sonar-pr.yml) computes
    changed_files relative to project-base-dir, matching how findings' paths
    are normalized, not the repo root."""
    return [f for f in findings if f["path"] in changed_files]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="Path to baseline findings JSON")
    parser.add_argument("--head", required=True, help="Path to head findings JSON")
    parser.add_argument("--out", required=True, help="Path to write new-findings JSON to")
    parser.add_argument(
        "--changed-files",
        help=(
            "Optional path to a newline-delimited list of files touched by the PR, "
            "in the same path-space as findings' `path` (i.e. relative to "
            "project-base-dir, not necessarily the repo root). When given, new "
            "findings outside this set are dropped."
        ),
    )
    parser.add_argument(
        "--removed-out",
        help=(
            "Optional path to write resolved-findings JSON to (findings present in "
            "baseline that are no longer in head). Informational only - not scoped "
            "by --changed-files, since a resolved finding's own file may have been "
            "deleted or moved rather than edited."
        ),
    )
    args = parser.parse_args()

    with open(args.baseline) as f:
        baseline = json.load(f)
    with open(args.head) as f:
        head = json.load(f)

    new_findings = diff(baseline, head)

    if args.changed_files:
        with open(args.changed_files) as f:
            changed_files = {line.strip() for line in f if line.strip()}
        new_findings = filter_by_changed_files(new_findings, changed_files)

    with open(args.out, "w") as f:
        json.dump(new_findings, f, indent=2, sort_keys=True)
        f.write("\n")

    if args.removed_out:
        removed_findings = diff(head, baseline)
        with open(args.removed_out, "w") as f:
            json.dump(removed_findings, f, indent=2, sort_keys=True)
            f.write("\n")

    print(
        f"{len(new_findings)} new finding(s) out of {len(head)} head finding(s) "
        f"(baseline had {len(baseline)})"
    )


if __name__ == "__main__":
    main()
