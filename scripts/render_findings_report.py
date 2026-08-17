#!/usr/bin/env python3
"""Render a human-readable Markdown report of every finding in a normalized
findings JSON file (fetch_findings.py's output schema), with each finding's
exact location (path:line).

Why this exists: the SonarQube instance this action spins up is ephemeral -
torn down at the end of the job - so once the job finishes there is no
SonarQube UI left to browse for issue detail. Check Run annotations already
cover *new* findings inline-on-diff for PRs; this report covers the full
current finding set (matching fetch_findings.py's whole output) as a
durable, downloadable artifact - the companion a user needs to audit
everything currently reported, not just what changed in one PR.
"""
import argparse
import json

# Legacy severity scale, high to low - worst findings first in the report.
SEVERITY_ORDER = ["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]


def _sort_key(finding):
    severity = finding.get("severity")
    # Unrecognized/missing severities sort last, not first - never let a
    # malformed finding hide real BLOCKER/CRITICAL rows above it.
    severity_rank = SEVERITY_ORDER.index(severity) if severity in SEVERITY_ORDER else len(SEVERITY_ORDER)
    return (severity_rank, finding.get("path") or "", finding.get("line") or 0)


def _escape_for_table_cell(text):
    """Markdown table cells break on literal `|` and newlines - a SonarQube
    finding message can contain either."""
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def render_report(findings, project_key):
    lines = [f"# SAST SonarQube findings report - `{project_key}`", ""]

    if not findings:
        lines.append("No findings.")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"{len(findings)} finding(s).")
    lines += ["", "| Severity | Type | Rule | Location | Message |", "| --- | --- | --- | --- | --- |"]
    for finding in sorted(findings, key=_sort_key):
        path = finding.get("path") or ""
        line = finding.get("line")
        location = f"{path}:{line}" if line else path
        finding_type = "Hotspot" if finding.get("type") == "SECURITY_HOTSPOT" else "Issue"
        lines.append(
            f"| {finding.get('severity', '')} | {finding_type} | {finding.get('rule', '')} "
            f"| `{location}` | {_escape_for_table_cell(finding.get('message'))} |"
        )
    lines.append("")
    return "\n".join(lines)


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--findings", required=True, help="Path to a normalized findings JSON file (fetch_findings.py's schema)")
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--out", required=True, help="Path to write the Markdown report to")
    args = parser.parse_args()

    with open(args.findings) as f:
        findings = json.load(f)

    report = render_report(findings, args.project_key)

    with open(args.out, "w") as f:
        f.write(report)

    print(f"Wrote findings report ({len(findings)} finding(s)) to {args.out}")


if __name__ == "__main__":
    main()
