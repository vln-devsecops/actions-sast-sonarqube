#!/usr/bin/env python3
"""Decide whether a `sonar-pr.yml` run may scan automatically, must block
until a maintainer dispatches it, or IS that maintainer dispatch - the
`scanner-mode: dotnet` trust-boundary gate (docs/csharp-support-plan.md,
"Trust boundary" and the 2026-11-12 decision).

`dotnet` mode runs build-command - the code under scan - on the runner (see
action.yml / the README's trust note), so it can only run automatically for
a pull request from a branch in the SAME repository. A pull request from a
fork gets `dotnet` mode only through a maintainer-triggered
`workflow_dispatch` naming the PR number AND the exact head SHA the
maintainer reviewed - see verify_dispatch_pr_context() below for why the
PR number alone is not enough. That run checks out and scans exactly that
reviewed commit and posts the named Check Run on its head SHA, so a branch
rule requiring that check only goes green after a real scan of reviewed
code. `cli` mode never executes PR code, so this gate never applies to it.

Kept as a small, pure, unit-tested decision function (decide()) plus a thin
CLI/network shell, rather than a complex inline workflow `if:` expression -
mirrors every other scripts/*.py in this repo (e.g. check_dotnet_projects.py).

Event-context note (verified against GitHub's docs, not assumed): inside a
workflow invoked via `workflow_call`, "the github context is always
associated with the caller workflow" -
https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations -
so github.event/github.event_name here are the CALLER's (e.g.
"pull_request"), never "workflow_call" itself. That is what makes reading
github.event.pull_request.* directly off the event payload here valid;
sonar-pr.yml's "Post PR summary comment" step already relied on the same
fact (github.event.pull_request.head.repo.full_name) before this script
existed. pull_request_target carries the identical payload shape as
pull_request (same github.event.pull_request object - see
https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows,
"pull_request_target" uses the "pull_request" webhook payload), so it's
handled by the exact same code path below, not a separate case - including
the fork-PR gate, which matters at least as much there (pull_request_target
callers typically want elevated, base-repo permissions against fork code,
which is a reason to apply the gate harder, not skip it). The one thing
the event payload can NOT carry is a workflow_dispatch run's PR number or
reviewed head SHA - on.workflow_call doesn't receive the caller's
github.event.inputs, so those come in as workflow_call inputs instead
(--dispatch-pr-number / --dispatch-head-sha below), resolved by the caller
from its own workflow_dispatch inputs.
"""
import argparse
import json
import os
import re
import sys

from gh_client import request as gh_api

ACTION_SCAN = "scan"
ACTION_BLOCK = "block"
ACTION_DISPATCH = "dispatch"
ACTION_ERROR = "error"

# A plain decimal PR number - no sign, no leading zero, no digit-group
# separator, no non-ASCII digit. int() accepts all of "+7", "07", "1_0" and
# "١٢٣" (Arabic-Indic digits) - every one of those is a workflow_call input
# a caller could pass in by mistake (or by a confused `gh` invocation), and
# none of them is a PR number GitHub would ever show.
PR_NUMBER_RE = re.compile(r"^[1-9][0-9]*$")
# A full, lower-case, 40-character git SHA-1 - what a maintainer copies
# from the PR's commit list, not an abbreviated SHA or a branch name.
HEAD_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# {pr_number} is filled in by decide() below. One line, no embedded
# newlines - this ends up in a GITHUB_OUTPUT value and a job summary line.
BLOCK_MESSAGE = (
    "scanner-mode: dotnet runs build-command - the code under scan - on the "
    "runner, so it cannot run automatically for a pull request from a fork "
    "(see the README's trust note). A maintainer must review the PR's head "
    "commit, then dispatch this workflow's workflow_dispatch trigger with "
    "pr-number={pr_number} and head-sha set to the exact SHA of the commit "
    "just reviewed (Actions tab -> this workflow -> Run workflow, or "
    "`gh workflow run <workflow-file> -f pr-number={pr_number} "
    "-f head-sha=<reviewed-commit-sha>`). That run re-checks the PR's LIVE "
    "head against head-sha before doing anything else - if a new commit "
    "landed after the review, it refuses and asks for a fresh one - then "
    "checks out and scans exactly the reviewed commit and posts the named "
    "Check Run on it, so a branch rule requiring that check only goes green "
    "after a real scan of reviewed code."
)

# pull_request_target carries the identical payload shape as pull_request -
# see the module docstring - so both are handled by the same code path.
_PULL_REQUEST_EVENT_NAMES = ("pull_request", "pull_request_target")


def _repo_full_name(repo):
    return (repo or {}).get("full_name")


def decide(event_name, event, scanner_mode, dispatch_pr_number, dispatch_head_sha=None):
    """Pure decision: no network, no filesystem access. `event` is the
    parsed GITHUB_EVENT_PATH payload ({} is fine for workflow_dispatch - its
    PR number/head SHA come from dispatch_pr_number/dispatch_head_sha, not
    the event payload; see the module docstring).

    Returns a dict with at least "action" (one of the ACTION_* constants
    above) and "reason" (human-readable, one line). The pull_request(_target)
    path also carries pr_number/is_fork/head_sha/base_sha/base_ref straight
    off the payload - no API call needed. The workflow_dispatch path only
    validates INPUT SHAPE here (pr_number/head_sha well-formed) - the live
    re-check against GitHub's current PR state happens in
    verify_dispatch_pr_context() below, which needs a network call main()
    makes, not something decide() can do on its own.
    """
    event = event or {}

    if event_name in _PULL_REQUEST_EVENT_NAMES:
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
        raw_pr = (dispatch_pr_number or "").strip()
        if not raw_pr:
            return {
                "action": ACTION_ERROR,
                "reason": "workflow_dispatch requires a pr-number input naming which PR to scan.",
            }
        if not PR_NUMBER_RE.match(raw_pr):
            return {
                "action": ACTION_ERROR,
                "reason": f"pr-number input '{raw_pr}' is not a plain positive integer (no sign, no leading zero, digits 0-9 only).",
            }
        pr_number = int(raw_pr)

        raw_sha = (dispatch_head_sha or "").strip()
        if not raw_sha:
            return {
                "action": ACTION_ERROR,
                "reason": "workflow_dispatch requires a head-sha input naming the exact commit a maintainer reviewed.",
            }
        if not HEAD_SHA_RE.match(raw_sha):
            return {
                "action": ACTION_ERROR,
                "reason": f"head-sha input '{raw_sha}' is not a 40-character lower-case hex commit SHA.",
            }

        return {
            "action": ACTION_DISPATCH,
            "reason": f"maintainer-triggered workflow_dispatch for PR #{pr_number} at reviewed head {raw_sha}.",
            "pr_number": pr_number,
            "expected_head_sha": raw_sha,
        }

    if event_name == "push":
        # sonar-pr.yml is never called on a push, but resolve this
        # permissively rather than with ACTION_ERROR: a push to a branch
        # already required the pusher to have write access, same trust
        # level as a same-repo PR, and this keeps the function total over
        # every event GitHub can hand a workflow.
        return {"action": ACTION_SCAN, "reason": "push event is not a pull request; the fork-PR gate does not apply."}

    return {"action": ACTION_ERROR, "reason": f"unsupported event_name '{event_name}' for the fork-PR gate."}


def verify_dispatch_pr_context(pr_number, expected_head_sha, pr_context):
    """Pure TOCTOU re-check for the workflow_dispatch path: a maintainer
    reviews a PR's head commit and dispatches a scan for it, but a fork can
    push a new, unreviewed commit after that review and before (or during)
    this run - without this check, THAT commit would be the one actually
    built with this job's write-scoped tokens, not the one anybody looked
    at. `pr_context` is fetch_pr_context()'s live API result (or an
    equivalent fake in tests) - this function does no network I/O itself,
    so every branch below is covered by a plain unit test.

    Returns None when the dispatch may proceed (the live head still matches
    what was reviewed, and the PR is still open), or a human-readable error
    string naming exactly what changed otherwise.
    """
    state = pr_context.get("state")
    head_sha = pr_context.get("head_sha")
    base_sha = pr_context.get("base_sha")
    base_ref = pr_context.get("base_ref")
    if not head_sha or not base_sha or not base_ref:
        return (
            f"PR #{pr_number}'s head_sha/base_sha/base_ref could not all be resolved from the "
            "GitHub API - refusing to dispatch against incomplete PR data."
        )
    if state != "open":
        return f"PR #{pr_number} is '{state}', not open - refusing to dispatch a scan for it."
    if head_sha != expected_head_sha:
        return (
            f"PR #{pr_number}'s head is now {head_sha}, not the reviewed {expected_head_sha} - "
            "a new commit landed after the review this dispatch was approved on. Review the PR "
            "again and dispatch with its new head SHA."
        )
    return None


def fetch_pr_context(repo, pr_number, token):  # pragma: no cover - network I/O, validated live
    """Resolve a PR number (the workflow_dispatch path - its event payload
    carries no pull_request data at all) to the same fields decide() reads
    straight off a pull_request event's payload, plus `state` (needed by
    verify_dispatch_pr_context() above - a closed/merged PR must not be
    dispatched). Always the PR's CURRENT state, not whatever it was when
    the dispatch was requested - that gap is exactly what
    verify_dispatch_pr_context() exists to close."""
    owner, name = repo.split("/", 1)
    pr = gh_api("GET", f"https://api.github.com/repos/{owner}/{name}/pulls/{pr_number}", token)
    head = pr.get("head") or {}
    base = pr.get("base") or {}
    head_repo_full_name = _repo_full_name(head.get("repo"))
    base_repo_full_name = _repo_full_name(base.get("repo"))
    return {
        "state": pr.get("state"),
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
    parser.add_argument(
        "--dispatch-head-sha", default="",
        help="workflow_call head-sha input - the exact commit SHA a maintainer reviewed before dispatching",
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

    result = decide(args.event_name, event, args.scanner_mode, args.dispatch_pr_number, args.dispatch_head_sha)

    if result["action"] == ACTION_DISPATCH:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise SystemExit("GITHUB_TOKEN environment variable is required to resolve --dispatch-pr-number")
        if not args.repo:
            raise SystemExit("--repo is required to resolve --dispatch-pr-number")
        pr_context = fetch_pr_context(args.repo, result["pr_number"], token)
        error = verify_dispatch_pr_context(result["pr_number"], result["expected_head_sha"], pr_context)
        if error:
            result = {"action": ACTION_ERROR, "reason": error, "pr_number": result["pr_number"]}
        else:
            result.update(pr_context)

    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")

    print(json.dumps(result, indent=2, sort_keys=True), file=sys.stderr)

    if result["action"] == ACTION_ERROR:
        sys.exit(1)


if __name__ == "__main__":
    main()
