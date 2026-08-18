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

from diff_findings import finding_key

PAGE_SIZE = 500  # documented maximum for `ps` on api/issues/search and api/hotspots/search
# Only issues a developer still needs to act on - resolved/closed issues
# (fixed, false positive, won't fix, ...) are not findings.
OPEN_STATUSES = "OPEN,CONFIRMED,REOPENED"
# Only hotspots still awaiting a human decision - REVIEWED ones (resolution
# FIXED/SAFE/ACKNOWLEDGED) are, like resolved issues, not findings.
HOTSPOT_STATUS = "TO_REVIEW"

# Index-backed search endpoints have historically refused to page beyond a
# fixed offset (p * ps), returning an error rather than more results. It is
# not in the current API docs and may no longer apply, but paging deep
# enough to find out would fail a whole baseline run - so treat the ceiling
# as real and partition around it. Costs one comparison on the happy path.
MAX_SEARCH_OFFSET = 10_000
MAX_PAGES = MAX_SEARCH_OFFSET // PAGE_SIZE

# Disjoint dimensions to split an over-large issues query along, most
# selective first. `severities` partitions the issue set 5 ways with no
# overlap and no gaps; `types` adds a further 3x if a single severity is
# still too large.
ISSUE_PARTITIONS = (
    ("severities", ("INFO", "MINOR", "MAJOR", "CRITICAL", "BLOCKER")),
    ("types", ("BUG", "VULNERABILITY", "CODE_SMELL")),
)
# api/hotspots/search has no severity/type facets to partition on, and
# `status` is already pinned to TO_REVIEW. If a project ever exceeds the
# pageable window in hotspots alone, the next dimension is `files`.
HOTSPOT_PARTITIONS = ()

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

VALID_SEVERITIES = ("INFO", "MINOR", "MAJOR", "CRITICAL", "BLOCKER")

# SonarQube should always populate a legacy `severity` for issues and a
# recognized `vulnerabilityProbability` for hotspots, but neither is
# guaranteed by the API contract - a field can be absent, and a future
# SonarQube can add a probability value this mapping doesn't know. A finding
# whose severity can't be determined must never silently vanish from a
# security gate, so it fails closed at the default blocking threshold
# instead of being emitted as null.
UNKNOWN_SEVERITY_FALLBACK = "MAJOR"


def _coerce_severity(severity, finding_desc):
    """Guarantee the findings schema's `severity` invariant: always one of
    VALID_SEVERITIES, never None. Anything unrecognized fails closed and is
    reported on stderr so it surfaces in the job log rather than silently
    changing the gate's behaviour."""
    if severity in VALID_SEVERITIES:
        return severity
    print(
        f"warning: {finding_desc} has unrecognized severity {severity!r}; "
        f"treating it as {UNKNOWN_SEVERITY_FALLBACK}",
        file=sys.stderr,
    )
    return UNKNOWN_SEVERITY_FALLBACK


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
    path = _strip_component_prefix(issue.get("component", ""), project_key)
    return {
        "type": "ISSUE",
        "rule": issue.get("rule"),
        "path": path,
        "hash": issue.get("hash") or None,
        "line": issue.get("line"),
        "severity": _coerce_severity(issue.get("severity"), f"issue {issue.get('rule')!r} in {path}"),
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
    path = _strip_component_prefix(hotspot.get("component", ""), project_key)
    probability = hotspot.get("vulnerabilityProbability")
    return {
        "type": "SECURITY_HOTSPOT",
        "rule": hotspot.get("ruleKey"),
        "path": path,
        "hash": None,
        "line": hotspot.get("line"),
        "severity": _coerce_severity(
            VULNERABILITY_PROBABILITY_TO_SEVERITY.get(probability),
            f"hotspot {hotspot.get('ruleKey')!r} in {path} "
            f"(vulnerabilityProbability={probability!r})",
        ),
        "impacts": [],
        "message": hotspot.get("message", ""),
    }


def _fetch_pages(host, token, path, params, items_key):  # pragma: no cover - network I/O, validated live
    """Page through a single query up to the offset ceiling.

    Returns (items, total). `total` is the server's count for the whole
    query, which may exceed len(items) when the ceiling was hit - the caller
    decides how to handle that rather than silently returning a partial
    set.
    """
    items = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        data = api_get(host, token, path, {**params, "ps": PAGE_SIZE, "p": page})
        page_items = data.get(items_key, [])
        items.extend(page_items)

        paging = data.get("paging", {})
        total = paging.get("total", len(items))
        page_size = paging.get("pageSize", PAGE_SIZE) or PAGE_SIZE
        if not page_items or page * page_size >= total:
            break

    return items, total


def plan_partitions(total, fetched, partitions):
    """Pure planning step for _fetch_exhaustive: decide what to do when a
    query's paged results (`fetched` items) fall short of its reported
    `total`.

    Returns None when paging already got everything (fetched >= total) - no
    partitioning needed. Otherwise returns (param, values, remaining), where
    the caller should issue one sub-query per entry in `values` (each with
    `param` set to that value), and `remaining` is what's left of
    `partitions` for those sub-queries to split along further if they're
    still too large. `values` is empty when no partitioning dimension is
    left - the caller should accept the partial result rather than recurse.
    """
    if fetched >= total:
        return None
    if not partitions:
        return ("", (), ())
    (param, values), *rest = partitions
    return (param, values, tuple(rest))


def _fetch_exhaustive(host, token, path, params, items_key, partitions):  # pragma: no cover - network I/O, validated live
    """Fetch every item for a query, splitting it along `partitions` when the
    result set is too large to page through in one go."""
    items, total = _fetch_pages(host, token, path, params, items_key)
    plan = plan_partitions(total, len(items), partitions)
    if plan is None:
        return items

    param, values, remaining = plan
    if not values:
        print(
            f"warning: {path} returned {total} results for {params!r} but only "
            f"{len(items)} could be paged through, and no further partitioning "
            f"dimension is available. Findings may be incomplete.",
            file=sys.stderr,
        )
        return items

    print(
        f"note: {path} has {total} results for {params!r}, beyond the pageable "
        f"window - splitting the query by {param}.",
        file=sys.stderr,
    )
    partitioned = []
    for value in values:
        partitioned.extend(
            _fetch_exhaustive(host, token, path, {**params, param: value}, items_key, remaining)
        )
    return partitioned


def _dedupe(findings):
    """Guard against double-counting across partitioned sub-queries. The
    partition dimensions (severity, type) are single-valued per issue so the
    sub-queries are disjoint in principle, but this makes that guarantee
    explicit rather than relying on it silently holding."""
    seen = set()
    deduped = []
    for finding in findings:
        key = finding_key(finding)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    return deduped


def fetch_all(host, token, project_key):  # pragma: no cover - network I/O, validated live
    issues = _fetch_exhaustive(
        host, token, "/api/issues/search",
        {"componentKeys": project_key, "statuses": OPEN_STATUSES}, "issues",
        ISSUE_PARTITIONS,
    )
    hotspots = _fetch_exhaustive(
        host, token, "/api/hotspots/search",
        {"project": project_key, "status": HOTSPOT_STATUS}, "hotspots",
        HOTSPOT_PARTITIONS,
    )
    return _dedupe(
        [normalize(i, project_key) for i in issues]
        + [normalize_hotspot(h, project_key) for h in hotspots]
    )


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
