#!/usr/bin/env python3
"""Decide whether a `sonar-pr.yml` run may scan automatically, must block
until a maintainer dispatches it, or IS that maintainer dispatch - the
`scanner-mode: dotnet` trust-boundary gate (docs/csharp-support-plan.md,
"Trust boundary" and the 2026-11-12 decision).

`dotnet` mode runs build-command - the code under scan - on the runner (see
action.yml / the README's trust note), so it can only run automatically for
a pull request from a branch in the SAME repository. A pull request from a
fork gets `dotnet` mode only through a maintainer-triggered
`workflow_dispatch` naming the PR number; that run checks out and scans the
PR's real head and posts the named Check Run on its head SHA, so a branch
rule requiring that check only goes green after a real scan actually ran.
`cli` mode never executes PR code, so this gate never applies to it.

Kept as a small, pure, unit-tested decision function (decide()) plus a thin
CLI/network shell, rather than a complex inline workflow `if:` expression -
mirrors every other scripts/*.py in this repo (e.g. check_dotnet_projects.py).

Event-context note (verified against GitHub's docs and community reports,
not assumed): inside a workflow invoked via `workflow_call`, `github.event`
and `github.event_name` are inherited from whatever triggered the overall
run (the CALLER's event - e.g. "pull_request"), never "workflow_call"
itself - see the README's "Reusable-workflow event context" note. That is
what makes reading `github.event.pull_request.*` directly off the event
payload here valid; sonar-pr.yml's "Post PR summary comment" step already
relied on the same fact (github.event.pull_request.head.repo.full_name)
before this script existed. The one thing the event payload can NOT carry
is a workflow_dispatch run's PR number - on.workflow_call doesn't receive
the caller's github.event.inputs, so that comes in as a workflow_call input
instead (--dispatch-pr-number below), resolved by the caller from its own
workflow_dispatch inputs.
"""
import argparse
import json
import os
import sys

from gh_client import request as gh_api

ACTION_SCAN = "scan"
ACTION_BLOCK = "block"
ACTION_DISPATCH = "dispatch"
ACTION_ERROR = "error"

# {pr_number} is filled in by decide() below. One line, no embedded
# newlines - this ends up in a GITHUB_OUTPUT value and a job summary line.
BLOCK_MESSAGE = (
    "scanner-mode: dotnet runs build-command - the code under scan - on the "
    "runner, so it cannot run automatically for a pull request from a fork "
    "(see the README's trust note). A maintainer must run the SAST workflow "
    "for this PR: dispatch this workflow's workflow_dispatch trigger with "
    "pr-number={pr_number} (Actions tab -> this workflow -> Run workflow, or "
    "`gh workflow run <workflow-file> -f pr-number={pr_number}`). That run "
    "checks out and scans the PR's real head and posts the named Check Run "
    "on its head SHA, so a branch rule requiring that check only goes green "
    "after a real scan."
)


def _repo_full_name(repo):
    return (repo or {}).get("full_name")


def decide(event_name, event, scanner_mode, dispatch_pr_number):
    """Pure decision: no network, no filesystem access. `event` is the
    parsed GITHUB_EVENT_PATH payload ({} is fine for workflow_dispatch - its
    PR number comes from dispatch_pr_number, not the event payload; see the
    module docstring).

    Returns a dict with at least "action" (one of the ACTION_* constants
    above) and "reason" (human-readable, one line). The "pull_request"
    path also carries pr_number/is_fork/head_sha/base_sha/base_ref straight
    off the payload - no API call needed, unlike the "workflow_dispatch"
    path (see fetch_pr_context below, which the CLI below calls to fill
    those same fields in for a dispatch run).
    """
    event = event or {}

    if event_name == "pull_request":
        pr = event.get("pull_request")
        if not isinstance(pr, dict):
            return {
                "action": ACTION_ERROR,
                "reason": "pull_request event payload is missing a 'pull_request' object.",
            }
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        head_repo_full_name = _repo_full_name(head.get("repo"))
        base_repo_full_name = _repo_full_name(base.get("repo"))
        # A deleted fork's repo comes back null in the payload - treat that
        # as a fork (can't confirm same-repo trust) rather than crash or,
        # worse, assume trusted because the comparison came back equal-ish.
        is_fork = head_repo_full_name is None or head_repo_full_name != base_repo_full_name
        pr_number = pr.get("number", event.get("number"))
        result = {
            "pr_number": pr_number,
            "is_fork": is_fork,
            "head_sha": head.get("sha"),
            "base_sha": base.get("sha"),
            "base_ref": base.get("ref"),
        }
        if scanner_mode != "dotnet":
            result.update(
                action=ACTION_SCAN,
                reason="scanner-mode is not dotnet; this gate only applies to dotnet mode (cli mode never executes PR code).",
            )
        elif not is_fork:
            result.update(
                action=ACTION_SCAN,
                reason="pull request from a branch in the same repository; trusted author.",
            )
        else:
            result.update(action=ACTION_BLOCK, reason=BLOCK_MESSAGE.format(pr_number=pr_number))
        return result

    if event_name == "workflow_dispatch":
        raw = (dispatch_pr_number or "").strip()
        if not raw:
            return {
                "action": ACTION_ERROR,
                "reason": "workflow_dispatch requires a pr-number input naming which PR to scan.",
            }
        try:
            pr_number = int(raw)
        except ValueError:
            return {"action": ACTION_ERROR, "reason": f"pr-number input '{raw}' is not an integer."}
        if pr_number <= 0:
            return {"action": ACTION_ERROR, "reason": f"pr-number input '{raw}' must be a positive PR number."}
        return {
            "action": ACTION_DISPATCH,
            "reason": f"maintainer-triggered workflow_dispatch for PR #{pr_number}.",
            "pr_number": pr_number,
        }

    if event_name == "push":
        # sonar-pr.yml is never called on a push, but resolve this
        # permissively rather than with ACTION_ERROR: a push to a branch
        # already required the pusher to have write access, same trust
        # level as a same-repo PR, and this keeps the function total over
        # every event GitHub can hand a workflow.
        return {"action": ACTION_SCAN, "reason": "push event is not a pull request; the fork-PR gate does not apply."}

    return {"action": ACTION_ERROR, "reason": f"unsupported event_name '{event_name}' for the fork-PR gate."}


def fetch_pr_context(repo, pr_number, token):  # pragma: no cover - network I/O, validated live
    """Resolve a PR number (the workflow_dispatch path - its event payload
    carries no pull_request data at all) to the same fields decide() reads
    straight off a pull_request event's payload. Always the PR's CURRENT
    head, not whatever it was when the dispatch was requested."""
    owner, name = repo.split("/", 1)
    pr = gh_api("GET", f"https://api.github.com/repos/{owner}/{name}/pulls/{pr_number}", token)
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo_full_name = _repo_full_name(head.get("repo"))
    base_repo_full_name = _repo_full_name(base.get("repo"))
    return {
        "is_fork": head_repo_full_name is None or head_repo_full_name != base_repo_full_name,
        "head_sha": head.get("sha"),
        "base_sha": base.get("sha"),
        "base_ref": base.get("ref"),
    }


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True, help="github.event_name")
    parser.add_argument(
        "--event-file", default="",
        help="Path to the GITHUB_EVENT_PATH JSON payload (omit/empty for workflow_dispatch)",
    )
    parser.add_argument("--scanner-mode", required=True, choices=["cli", "dotnet"])
    parser.add_argument(
        "--dispatch-pr-number", default="",
        help="workflow_call pr-number input, wired by the caller from its own workflow_dispatch inputs",
    )
    parser.add_argument("--repo", default="", help="owner/repo - required to resolve --dispatch-pr-number via the API")
    parser.add_argument("--out", required=True, help="Path to write the decision JSON to")
    args = parser.parse_args()

    event = {}
    if args.event_file:
        try:
            with open(args.event_file, encoding="utf-8") as f:
                event = json.load(f)
        except OSError:
            event = {}

    result = decide(args.event_name, event, args.scanner_mode, args.dispatch_pr_number)

    if result["action"] == ACTION_DISPATCH:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit("GITHUB_TOKEN environment variable is required to resolve --dispatch-pr-number")
        if not args.repo:
            raise SystemExit("--repo is required to resolve --dispatch-pr-number")
        result.update(fetch_pr_context(args.repo, result["pr_number"], token))

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")

    print(json.dumps(result, indent=2, sort_keys=True), file=sys.stderr)

    if result["action"] == ACTION_ERROR:
        sys.exit(1)


if __name__ == "__main__":
    main()
