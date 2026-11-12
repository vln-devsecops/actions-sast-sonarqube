#!/usr/bin/env python3
"""Classify a scanner-mode: dotnet failure-reason code (see run_scan.sh's
`fail()` calls) as "the build itself failed" or "something else failed" -
used by sonar-pr.yml's fallback baseline-resolution path (docs/csharp-support-plan.md,
the 2026-11-12 decision: "Fallback baseline in dotnet mode").

Why this needs to be its own decision, not "any failure means empty
baseline": the fallback path scans the TARGET branch - code nobody is
proposing to merge right now - as a stand-in baseline when no artifact
exists yet. If the target branch genuinely doesn't build in dotnet mode
(stale history, a solution added after that commit, a flaky test project),
treating every head finding as new is the right call and better than
failing the PR over code it didn't touch. But a failure reason that has
nothing to do with the build - the ephemeral SonarQube instance itself
going down, a bad scanner install, a malformed .sastrc - must NOT be
silently reinterpreted as "the base doesn't build": that would hide a real
infrastructure problem behind a baseline that looks merely permissive.

Kept as a tiny, pure, unit-tested lookup rather than an inline bash/jq
condition, mirroring resolve_pr_context.py's decide().
"""
import argparse
import sys

# Reason codes from run_scan.sh / check_dotnet_projects.py that mean "the
# build did not produce an analyzable result", not "something around the
# build broke". Deliberately NOT included: begin-failed (the scanner
# couldn't even start - usually a reachability/config problem, not the
# target branch's code), end-failed (the generic case - no-analyzable-projects
# is the specific sub-case of "end failed" that IS a build-failure reason,
# so a bare end-failed is left as "not a build failure"), scanner-install-failed,
# no-report-task, ce-task-failed, ce-task-timeout, invalid-sast-config,
# build-command-required/-not-allowed, invalid-scanner-mode,
# project-base-dir-missing, scanner-failed (cli mode has no fallback-build
# concept at all).
BUILD_FAILURE_REASONS = frozenset(
    {
        "build-command-failed",
        "no-analyzable-projects",
        "project-not-built",
        "project-not-analyzed",
        "check-failed",
    }
)


def is_build_failure_reason(reason):
    """True when `reason` (a run_scan.sh failure-reason code) means the
    target branch's own build/analysis failed, as opposed to an
    infrastructure or configuration problem unrelated to that code."""
    return (reason or "").strip() in BUILD_FAILURE_REASONS


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reason", default="", help="run_scan.sh failure-reason code to classify")
    args = parser.parse_args()
    print("true" if is_build_failure_reason(args.reason) else "false")
    sys.exit(0)


if __name__ == "__main__":
    main()
