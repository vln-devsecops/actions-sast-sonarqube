#!/usr/bin/env python3
"""Fetch every open issue for a SonarQube project and normalize it into the
findings schema shared across this action (baseline artifacts, fallback
scans, and head scans all produce this same shape):

    {
      "rule": "string",
      "path": "string (relative to repo root)",
      "hash": "string|null",
      "line": "int|null",
      "severity": "BLOCKER|CRITICAL|MAJOR|MINOR|INFO",
      "impacts": [{"softwareQuality": "...", "severity": "..."}],
      "message": "string"
    }
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


def api_get(host, token, path, params):
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


def normalize(issue, project_key):
    component = issue.get("component", "")
    prefix = f"{project_key}:"
    path = component[len(prefix):] if component.startswith(prefix) else component
    return {
        "rule": issue.get("rule"),
        "path": path,
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


def fetch_all(host, token, project_key):
    findings = []
    page = 1
    while True:
        data = api_get(
            host,
            token,
            "/api/issues/search",
            {
                "componentKeys": project_key,
                "statuses": OPEN_STATUSES,
                "ps": PAGE_SIZE,
                "p": page,
            },
        )
        issues = data.get("issues", [])
        findings.extend(normalize(issue, project_key) for issue in issues)

        paging = data.get("paging", {})
        total = paging.get("total", len(findings))
        page_size = paging.get("pageSize", PAGE_SIZE)
        if not issues or page * page_size >= total:
            break
        page += 1

    return findings


def main():
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
