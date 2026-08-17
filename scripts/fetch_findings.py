#!/usr/bin/env python3
"""Fetch every open issue and unreviewed Security Hotspot for a SonarQube
project and normalize both into the findings schema shared across this
action (baseline artifacts, fallback scans, and head scans all produce this
same shape):

    {
      "type": "ISSUE|SECURITY_HOTSPOT",
      "rule": "string",
      "path": "string (relative to repo root)",
      "hash": "string|null",
      "line": "int|null",
      "severity": "BLOCKER|CRITICAL|MAJOR|MINOR|INFO",
      "impacts": [{"softwareQuality": "...", "severity": "..."}],
      "message": "string"
    }

Security Hotspots come from a separate API (api/hotspots/search) with a
different shape: no `hash` (so hotspots always fall back to line-based
matching in diff_findings.py), and no `severity`/`impacts` - instead a
`vulnerabilityProbability` (HIGH/MEDIUM/LOW), which is mapped onto the same
severity scale issues use (see VULNERABILITY_PROBABILITY_TO_SEVERITY) so the
rest of the pipeline (diffing, blocking policy, annotations) doesn't need to
know hotspots exist as a separate concept.
"""
import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

PAGE_SIZE = 500
# Only issues a developer still needs to act on - resolved/closed issues
# (fixed, false positive, won't fix, ...) are not findings.
OPEN_STATUSES = "OPEN,CONFIRMED,REOPENED"
# Only hotspots still awaiting a human decision - REVIEWED ones (resolution
# FIXED/SAFE/ACKNOWLEDGED) are, like resolved issues, not findings.
HOTSPOT_STATUS = "TO_REVIEW"

# SonarQube gives hotspots no severity of their own, just a
# vulnerabilityProbability. There's no canonical mapping onto the legacy
# severity scale issues use, so this is a judgment call: HIGH and MEDIUM are
# treated as at least worth blocking on by default (MAJOR threshold),
# LOW is not.
VULNERABILITY_PROBABILITY_TO_SEVERITY = {
    "HIGH": "CRITICAL",
    "MEDIUM": "MAJOR",
    "LOW": "MINOR",
}


def api_get(host, token, path, params):  # pragma: no cover - network I/O, validated live
    url = f"{host.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url)
    auth = base64.b64encode(f"{token}:".encode()).decode()
    req.add_header("Authorization", f"Basic {auth}")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"SonarQube API request to {path} failed: HTTP {e.code}\n{body}")


def _strip_component_prefix(component, project_key):
    prefix = f"{project_key}:"
    return component[len(prefix):] if component.startswith(prefix) else component


def normalize(issue, project_key):
    return {
        "type": "ISSUE",
        "rule": issue.get("rule"),
        "path": _strip_component_prefix(issue.get("component", ""), project_key),
        "hash": issue.get("hash") or None,
        "line": issue.get("line"),
        "severity": issue.get("severity"),
        "impacts": [
            {
                "softwareQuality": impact.get("softwareQuality"),
                "severity": impact.get("severity"),
            }
            for impact in issue.get("impacts", [])
        ],
        "message": issue.get("message", ""),
    }


def normalize_hotspot(hotspot, project_key):
    return {
        "type": "SECURITY_HOTSPOT",
        "rule": hotspot.get("ruleKey"),
        "path": _strip_component_prefix(hotspot.get("component", ""), project_key),
        "hash": None,
        "line": hotspot.get("line"),
        "severity": VULNERABILITY_PROBABILITY_TO_SEVERITY.get(hotspot.get("vulnerabilityProbability")),
        "impacts": [],
        "message": hotspot.get("message", ""),
    }


def _fetch_paginated(host, token, path, base_params, items_key):  # pragma: no cover - network I/O, validated live
    items = []
    page = 1
    while True:
        data = api_get(host, token, path, {**base_params, "ps": PAGE_SIZE, "p": page})
        page_items = data.get(items_key, [])
        items.extend(page_items)

        paging = data.get("paging", {})
        total = paging.get("total", len(items))
        page_size = paging.get("pageSize", PAGE_SIZE)
        if not page_items or page * page_size >= total:
            break
        page += 1

    return items


def fetch_all(host, token, project_key):  # pragma: no cover - network I/O, validated live
    issues = _fetch_paginated(
        host, token, "/api/issues/search",
        {"componentKeys": project_key, "statuses": OPEN_STATUSES}, "issues",
    )
    hotspots = _fetch_paginated(
        host, token, "/api/hotspots/search",
        {"project": project_key, "status": HOTSPOT_STATUS}, "hotspots",
    )
    return [normalize(i, project_key) for i in issues] + [
        normalize_hotspot(h, project_key) for h in hotspots
    ]


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="SonarQube base URL")
    parser.add_argument("--token", required=True, help="SonarQube user token")
    parser.add_argument("--project-key", required=True)
    parser.add_argument("--out", required=True, help="Path to write normalized findings JSON to")
    args = parser.parse_args()

    findings = fetch_all(args.host, args.token, args.project_key)

    with open(args.out, "w") as f:
        json.dump(findings, f, indent=2, sort_keys=True)
        f.write("\n")

    print(f"Wrote {len(findings)} finding(s) to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
