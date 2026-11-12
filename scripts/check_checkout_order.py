#!/usr/bin/env python3
"""Statically verify that no step in a reusable workflow's job can use this
action's own trusted source (checked out to `.sast-sonarqube`) after a
ROOT-level checkout could have wiped it.

Why this is a real, previously-live bug, not a theoretical one: actions/checkout,
when its target directory doesn't already contain a `.git` matching what it
expects, deletes EVERY entry under that directory before checking out -
see `git-directory-helper.ts`'s `prepareExistingDirectory()`:
`remove = true` whenever `repositoryPath/.git` doesn't exist, then
`for (const file of await fs.promises.readdir(repositoryPath)) await io.rmRF(...)`.
A checkout step with no `with.path` (or `path: "."`) targets the WORKSPACE
ROOT. `sonar-pr.yml` checks out this action's own source into the
`.sast-sonarqube` subdirectory of that same root BEFORE the workspace root
itself ever gets a `.git` - so when "Checkout PR head" (no `path:`) later
targets the root, the root still has no `.git`, `remove` is true, and EVERY
entry under the root - including the `.sast-sonarqube` subdirectory - gets
deleted. Two ways that bites:

- Every ordinary run breaks: `.sast-sonarqube/scripts/*.py`,
  `uses: ./.sast-sonarqube`, and its `docker-compose.ephemeral.yml` are
  gone by the time anything downstream needs them.
- A PR (including an automatic fork PR in cli mode, which this gate never
  blocks) can commit its OWN `.sast-sonarqube/` tree. Once the real one is
  wiped, nothing stops a later `uses: ./.sast-sonarqube` from resolving to
  the PR-supplied copy instead - arbitrary code execution on the runner,
  independent of whatever token permissions that run happens to have
  (resolve_pr_context.py separately refuses to run this gate at all for
  pull_request_target, which would have made this worse by handing that
  code a write-scoped token too).

The fix pattern this check enforces: re-run "Checkout this action's
source" immediately after any root-level checkout, before anything else
touches `.sast-sonarqube` again. Kept as a small, pure, unit-tested
function (find_checkout_order_violations()) over the parsed step list,
not a string grep over the YAML text - a grep can't tell "before" from
"after", which is the entire question here.
"""
import argparse
import sys

import yaml

ACTION_SOURCE_PATH = ".sast-sonarqube"


def _is_checkout_step(step):
    uses = (step or {}).get("uses", "")
    return isinstance(uses, str) and uses.startswith("actions/checkout@")


def _checkout_path(step):
    """The effective target directory of a checkout step's `with.path` -
    "" for the workspace root (unset, ".", or "./"), otherwise the given
    path with any trailing slash stripped."""
    with_block = (step or {}).get("with") or {}
    path = with_block.get("path")
    if path is None:
        return ""
    path = str(path).strip()
    if path in (".", "./"):
        return ""
    return path.rstrip("/")


def _is_action_source_checkout(step):
    """A trusted checkout of this action's own source, into ACTION_SOURCE_PATH."""
    return _is_checkout_step(step) and _checkout_path(step) == ACTION_SOURCE_PATH


def _is_root_checkout(step):
    """A checkout targeting the workspace root - the only kind that can
    wipe a sibling directory like ACTION_SOURCE_PATH (a checkout targeting
    any OTHER named subdirectory only ever deletes contents under that one
    subdirectory - see prepareExistingDirectory() in the module docstring)."""
    return _is_checkout_step(step) and _checkout_path(step) == ""


def _uses_action_source(step):
    """True if this step's own `run:`/`uses:` references the action's
    checked-out source - either a composite-action call
    (`uses: ./.sast-sonarqube...`) or a script/file invocation inside a
    `run:` block (`.sast-sonarqube/scripts/...`,
    `.sast-sonarqube/docker-compose.ephemeral.yml`, ...). A broad substring
    match is deliberate: a false positive only adds an extra "must follow a
    trusted checkout" requirement, never hides a real one."""
    uses = (step or {}).get("uses", "")
    if isinstance(uses, str) and uses.startswith(f"./{ACTION_SOURCE_PATH}"):
        return True
    run = (step or {}).get("run", "")
    return isinstance(run, str) and ACTION_SOURCE_PATH in run


def find_checkout_order_violations(steps):
    """Return the names (or `step[i]` when a step has no `name`) of every
    step that uses ACTION_SOURCE_PATH (per _uses_action_source) without a
    trusted checkout of it (per _is_action_source_checkout) having
    happened more recently than any root-level checkout (per
    _is_root_checkout) - i.e. a root checkout could have wiped it since
    the last time it was actually fetched.

    This is a STRUCTURAL check over the step list in file order, not a
    simulation of which steps actually run at a given `if:` - the
    invariant has to hold regardless of which conditional branch fires,
    so a step behind an `if:` is still counted on its position alone.
    """
    violations = []
    trusted = False
    for index, step in enumerate(steps):
        if _is_action_source_checkout(step):
            trusted = True
        elif _is_root_checkout(step):
            trusted = False
        if _uses_action_source(step) and not trusted:
            violations.append((step or {}).get("name") or f"step[{index}]")
    return violations


def main():  # pragma: no cover - CLI glue over the above, covered by tests/test_check_checkout_order.py
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", help="Path to a workflow YAML file")
    parser.add_argument("--job", required=True, help="Job id (key under jobs:) to check")
    args = parser.parse_args()

    with open(args.workflow, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    steps = doc["jobs"][args.job]["steps"]
    violations = find_checkout_order_violations(steps)
    if violations:
        print(
            f"{args.workflow}:{args.job}: these steps use {ACTION_SOURCE_PATH} "
            "without a trusted checkout more recent than any root-level checkout:",
            file=sys.stderr,
        )
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        sys.exit(1)
    print(f"{args.workflow}:{args.job}: checkout order OK ({len(steps)} steps).")


if __name__ == "__main__":
    main()
