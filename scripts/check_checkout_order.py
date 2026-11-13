#!/usr/bin/env python3
"""Statically verify that no step in a reusable workflow's job can use this
action's own trusted source (checked out to `.sast-sonarqube`) after a
ROOT-level checkout could have wiped it, and that no trusted step writes an
intermediate file into the (PR-controlled, once "Checkout PR head" has run)
workspace root.

Why this is a real, previously-live bug, not a theoretical one: actions/checkout,
when its target directory doesn't already contain a `.git` matching what it
expects, deletes EVERY entry under that directory before checking out -
see `git-directory-helper.ts`'s `prepareExistingDirectory()`:
`remove = true` whenever `repositoryPath/.git` doesn't exist, then
`for (const file of await fs.promises.readdir(repositoryPath)) await io.rmRF(...)`.
A checkout step with no `with.path` (or `path: "."`/`"./"`/`${{ github.workspace }}`)
targets the WORKSPACE ROOT. `sonar-pr.yml` checks out this action's own source
into the `.sast-sonarqube` subdirectory of that same root BEFORE the workspace
root itself ever gets a `.git` - so when "Checkout PR head" (no `path:`) later
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
touches `.sast-sonarqube` again - and, per find_mismatched_recheckout_conditions()
below, gated on the exact same `if:` condition as the checkout that
necessitated it, so the re-checkout can never fail to fire on an occasion
the wipe actually happened.

A second, related risk the PR tree itself can exploit even without any
wipe: a hostile PR can pre-commit its OWN file (or a symlink to one) at
the exact bare, relative path a trusted step later writes an intermediate
file to (e.g. `changed-files.txt`, `new-findings.json`,
`policy-result.json`) - a shell redirect or a script's `--out`/`--dest`
flag follows a symlink exactly like any other write, so a PR-committed
symlink there (e.g. to a runner-owned file, or `../../..`) redirects that
write wherever the PR chose. find_workspace_write_violations() below
flags any such write that isn't rooted under `$RUNNER_TEMP` (a runner-
managed scratch directory outside the checked-out repo tree entirely,
immune to anything a PR commits) or otherwise clearly safe (absolute,
`/dev/null`, `$GITHUB_OUTPUT`/`$GITHUB_STEP_SUMMARY`).

Kept as small, pure, unit-tested functions over the parsed step list, not
a string grep over the YAML text - a grep can't tell "before" from
"after", which is the entire question the first check answers.
"""
import argparse
import re
import sys

import yaml

ACTION_SOURCE_PATH = ".sast-sonarqube"

# Spelled with internal whitespace variations in the wild
# (${{github.workspace}}, ${{ github.workspace }}, ...) - normalized by
# stripping all whitespace before comparing in _checkout_path() below.
_GITHUB_WORKSPACE_EXPR = "${{github.workspace}}"


def _is_checkout_step(step):
    uses = (step or {}).get("uses", "")
    return isinstance(uses, str) and uses.startswith("actions/checkout@")


def _checkout_path(step):
    """The effective target directory of a checkout step's `with.path` -
    "" for the workspace root (unset, ".", "./", or the
    ${{ github.workspace }} expression in any whitespace variant),
    otherwise the given path with a leading "./" and any trailing slash
    stripped."""
    with_block = (step or {}).get("with") or {}
    path = with_block.get("path")
    if path is None:
        return ""
    path = str(path).strip()
    if path in (".", "./") or path.replace(" ", "") == _GITHUB_WORKSPACE_EXPR:
        return ""
    if path.startswith("./"):
        path = path[2:]
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


def _all_string_values(obj):
    """Yield every string anywhere inside a (possibly nested) step mapping
    - its `run`/`uses`/`working-directory`/`if`/`name`, and every value
    under `with:`/`env:` (including nested ones, for generality)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _all_string_values(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _all_string_values(value)


# A step whose ENTIRE `run:` body is one or more lines, each one a plain
# `rm -rf [--] <path> [<path> ...]` - no trailing `&&`, no second command
# of any other kind. Used both to exempt such a step from
# _uses_action_source()'s broad scan below, and by
# find_unprotected_subdirectory_checkouts() to recognize that a given
# checkout target was cleared first. The path-list capture excludes `&`,
# `|`, and `;`, so a line that combines a removal with anything else
# ("rm -rf -- X && cat Y") doesn't match here at all (the capture can't
# reach end-of-line without crossing one of those characters) and falls
# through to the broader substring rule instead of being misread as
# "removes X, Y, &&, and cat".
_REMOVAL_LINE_RE = re.compile(r"^rm\s+-rf\s+(?:--\s+)?([^&|;]+)$")


def _removed_paths(step):
    """Return the set of workspace-relative paths (normalized the same
    way _checkout_path() normalizes a checkout's `with.path`) a step's
    `run:` text consists ENTIRELY of removing - empty when the step isn't
    a pure removal at all (any line that doesn't match _REMOVAL_LINE_RE
    disqualifies the whole step, not just that line)."""
    run = (step or {}).get("run", "")
    if not isinstance(run, str):
        return set()
    lines = [line.strip() for line in run.strip().splitlines() if line.strip()]
    if not lines:
        return set()
    paths = set()
    for line in lines:
        match = _REMOVAL_LINE_RE.match(line)
        if not match:
            return set()
        for raw_path in match.group(1).split():
            path = raw_path
            if path.startswith("./"):
                path = path[2:]
            paths.add(path.rstrip("/"))
    return paths


def _is_pure_action_source_removal(step):
    return ACTION_SOURCE_PATH in _removed_paths(step)


def _uses_action_source(step):
    """True if ANYTHING in this step - `run:`, `uses:`, `working-directory:`,
    any `with:`/`env:` value, anywhere - references the action's checked-out
    source. A broad, whole-step substring scan is deliberate: a false
    positive only adds an extra "must follow a trusted checkout"
    requirement, never hides a real one - except for a step that's purely
    removing ACTION_SOURCE_PATH itself (see _is_pure_action_source_removal):
    deleting a path reads and executes nothing under it, trusted or not."""
    if _is_pure_action_source_removal(step):
        return False
    return any(ACTION_SOURCE_PATH in value for value in _all_string_values(step or {}))


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


def find_mismatched_recheckout_conditions(steps):
    """Return (checkout_name, recheckout_name) pairs where a root-level
    checkout is paired with a trusted action-source checkout - the fix
    pattern find_checkout_order_violations() above expects - but the two
    steps don't share the EXACT same `if:` condition. If they drift, the
    re-checkout could stop firing on an occasion the root checkout - and
    so the wipe - actually happens, silently reopening the hole the pair
    exists to close.

    "Paired with" means the nearest subsequent CHECKOUT step, skipping
    over any non-checkout steps in between (e.g. a cleanup step removing
    a symlink before the re-checkout runs) - but stopping, without
    comparing anything, at the first checkout found that ISN'T the
    action-source one: that's not this fix pattern, so there's nothing to
    assert about it here.
    """
    violations = []
    for index, step in enumerate(steps):
        step = step or {}
        if not _is_root_checkout(step):
            continue
        for later_step in steps[index + 1 :]:
            later_step = later_step or {}
            if not _is_checkout_step(later_step):
                continue
            if _is_action_source_checkout(later_step) and step.get("if") != later_step.get("if"):
                violations.append((step.get("name") or f"step[{index}]", later_step.get("name") or "re-checkout"))
            break
    return violations


def find_unprotected_subdirectory_checkouts(steps):
    """Return the names (or `step[i]`) of every checkout step targeting a
    fixed, non-root workspace subdirectory (per _checkout_path()) that
    runs after a root-level checkout (per _is_root_checkout - that's what
    first puts caller-controlled content on disk) without being preceded,
    anywhere from that point on, by a step purely removing that exact
    path first (per _removed_paths()). A checkout that happens BEFORE any
    root checkout (e.g. this action's own first checkout of its source)
    doesn't need one: nothing caller-controlled is on disk yet.

    Generalizes the .sast-sonarqube re-checkout's own removal step (the
    original, specific fix) to every such checkout - e.g. sonar-pr.yml's
    `.baseline-checkout` fallback target too.

    Unlike find_mismatched_recheckout_conditions() above, "preceded by"
    here means anywhere from the first root checkout onward, not
    immediately before: nothing recreates a removed path on its own, so
    the gap between the removal and the checkout it protects doesn't
    matter the way it does for the root-checkout/re-checkout `if:`
    pairing.
    """
    violations = []
    removed_so_far = set()
    root_checkout_seen = False
    for index, step in enumerate(steps):
        step = step or {}
        removed_so_far |= _removed_paths(step)
        path = _checkout_path(step)
        if _is_checkout_step(step):
            if root_checkout_seen and path and path not in removed_so_far:
                violations.append(step.get("name") or f"step[{index}]")
            if not path:
                root_checkout_seen = True
    return violations


# --- workspace-root write detection ---------------------------------------------

# A target token: either a double-quoted string (captured without its
# quotes) or an unquoted run of characters that can't be part of shell
# syntax ending that token - whitespace, a closing quote, parens (command
# substitution), a backslash (line continuation), `&`/`|`/`;` (control
# operators, so "2>&1" can never capture "&1" as a target).
_TOKEN = r'(?:"([^"]+)"|([^\s")(\\&|;]+))'

# Long output-ish flags, both `--flag value` and `--flag=value`.
_LONG_WRITE_FLAGS = ("out", "dest", "result-out", "removed-out", "output")
_WRITE_FLAG_RE = re.compile(r"(?<!\S)--(?:" + "|".join(_LONG_WRITE_FLAGS) + r")(?:=|\s+)" + _TOKEN)
# Short `-o value` / `-o=value` - excluding `set -o <shell-option>`
# (several steps in this repo's own workflows use `set -o pipefail`,
# `set -euo pipefail`, etc., none of which write a file named after the
# option), which is the one common idiom this shape would otherwise
# misread as a write.
_SHORT_WRITE_FLAG_RE = re.compile(r"(?<!\S)(?<!set )-o(?:=|\s+)" + _TOKEN)
# >, >>, >| (force/clobber); deliberately NOT preceded-by-check for "2>"
# etc. - a target starting with "/dev/null" is safe anyway (see
# _is_safe_write_target), and fd-duplication targets like "&1" never
# produce a non-empty match because "&" is excluded from the unquoted
# token's character class.
_REDIRECT_RE = re.compile(r">(?:>|\|)?\s*" + _TOKEN)
# `tee`/`tee -a` and `cp`/`mv`: their own arguments, up to the next
# pipe/control operator or end of line, tokenized the same way.
_TEE_ARGS_RE = re.compile(r"(?<!\S)tee\b([^|;\n]*)")
_CP_MV_ARGS_RE = re.compile(r"(?<!\S)(?:cp|mv)\b([^|;\n]*)")

# These are either outside the checked-out repo tree entirely (RUNNER_TEMP,
# any absolute path), or GitHub-managed per-step files that are never a
# bare path under the repo root a PR could plant a same-named symlink at.
_SAFE_WRITE_PREFIXES = ("$RUNNER_TEMP", "${RUNNER_TEMP", "$GITHUB_OUTPUT", "${GITHUB_OUTPUT", "$GITHUB_STEP_SUMMARY", "${GITHUB_STEP_SUMMARY")


def _is_safe_write_target(target):
    if not target:
        return True
    if target == "/dev/null" or target.startswith("/"):
        return True
    if target.isdigit():
        # Not a real write target: `((` arithmetic comparisons like
        # `(( count > 1 ))` match the same ">" pattern a redirect would,
        # and nothing in this codebase's conventions ever redirects to a
        # bare numeric filename.
        return True
    return target.startswith(_SAFE_WRITE_PREFIXES)


def _plain_tokens(text):
    """Every token in `text` per _TOKEN, unquoted."""
    for match in re.finditer(_TOKEN, text):
        yield match.group(1) if match.group(1) is not None else match.group(2)


def _extract_targets(run_text):
    for pattern in (_WRITE_FLAG_RE, _SHORT_WRITE_FLAG_RE, _REDIRECT_RE):
        for match in pattern.finditer(run_text):
            yield match.group(1) if match.group(1) is not None else match.group(2)
    for match in _TEE_ARGS_RE.finditer(run_text):
        for token in _plain_tokens(match.group(1)):
            if not token.startswith("-"):
                yield token
    for match in _CP_MV_ARGS_RE.finditer(run_text):
        tokens = [t for t in _plain_tokens(match.group(1)) if not t.startswith("-")]
        if len(tokens) >= 2:
            # Only the LAST (destination) argument is a write target -
            # every earlier one is a source, read rather than written.
            yield tokens[-1]


# A plain `name=value` or `name="value"` assignment line - resolved below
# so a step that assigns a $RUNNER_TEMP-rooted path to a local variable
# once (for readability, instead of repeating the literal string on every
# use) isn't mistaken for writing to the bare "$varname" text itself.
# Deliberately simple (one line, no command substitution, no concatenation):
# anything more than that is left unresolved, which only makes this check
# MORE cautious (an unresolved $VAR never matches a safe prefix), never
# less.
_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_VAR_REF_RE = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")


def _local_assignments(run_text):
    assignments = {}
    for line in run_text.splitlines():
        match = _ASSIGNMENT_RE.match(line)
        if not match:
            continue
        name, value = match.groups()
        value = value.strip()
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        assignments[name] = value
    return assignments


def _resolve_target(target, assignments, _seen=frozenset()):
    """Follow a bare `$VAR`/`${VAR}` reference to its locally assigned
    value (per _local_assignments), a few hops at most; anything else -
    not a plain variable reference, or a variable never assigned in this
    same `run:` block - is returned unchanged."""
    match = _VAR_REF_RE.match(target)
    if not match:
        return target
    name = match.group(1)
    if name in _seen or name not in assignments:
        return target
    return _resolve_target(assignments[name], assignments, _seen | {name})


def find_workspace_write_violations(steps):
    """Return (step_name, target) pairs for every `run:` step that writes
    to a bare, workspace-root-relative target instead of one that's
    $RUNNER_TEMP-rooted, absolute, `/dev/null`, or a GitHub-managed
    per-step file.

    Flags shell redirects (`>`, `>>`, `>|`), `tee`/`tee -a`, `cp`/`mv`'s
    last argument, and output flags (`--out`, `--dest`, `--result-out`,
    `--removed-out`, `--output`, `-o`, each as `flag value` or
    `flag=value`) in `run:` blocks; other write forms need review - this
    is a heuristic text scan, not a shell interpreter.

    `target` in each returned pair is the RESOLVED path (see
    _resolve_target) when the `run:` text used a local variable, so a
    report is always a real path, not just "$foo"."""
    violations = []
    for index, step in enumerate(steps):
        run = (step or {}).get("run", "")
        if not isinstance(run, str):
            continue
        assignments = _local_assignments(run)
        for target in _extract_targets(run):
            resolved = _resolve_target(target, assignments)
            if not _is_safe_write_target(resolved):
                violations.append(((step or {}).get("name") or f"step[{index}]", resolved))
    return violations


def main():  # pragma: no cover - CLI glue over the above, covered by tests/test_check_checkout_order.py
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workflow", help="Path to a workflow YAML file")
    parser.add_argument("--job", required=True, help="Job id (key under jobs:) to check")
    args = parser.parse_args()

    with open(args.workflow, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    steps = doc["jobs"][args.job]["steps"]
    status = 0

    order_violations = find_checkout_order_violations(steps)
    if order_violations:
        status = 1
        print(
            f"{args.workflow}:{args.job}: these steps use {ACTION_SOURCE_PATH} "
            "without a trusted checkout more recent than any root-level checkout:",
            file=sys.stderr,
        )
        for v in order_violations:
            print(f"  - {v}", file=sys.stderr)

    condition_violations = find_mismatched_recheckout_conditions(steps)
    if condition_violations:
        status = 1
        print(f"{args.workflow}:{args.job}: mismatched if: between a checkout and its re-checkout:", file=sys.stderr)
        for checkout_name, recheckout_name in condition_violations:
            print(f"  - {checkout_name!r} vs {recheckout_name!r}", file=sys.stderr)

    write_violations = find_workspace_write_violations(steps)
    if write_violations:
        status = 1
        print(f"{args.workflow}:{args.job}: these steps write into the workspace root:", file=sys.stderr)
        for step_name, target in write_violations:
            print(f"  - {step_name!r}: {target}", file=sys.stderr)

    if status == 0:
        print(f"{args.workflow}:{args.job}: checkout order and workspace-write checks OK ({len(steps)} steps).")
    sys.exit(status)


if __name__ == "__main__":
    main()
