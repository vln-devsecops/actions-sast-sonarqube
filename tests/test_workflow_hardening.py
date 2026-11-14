"""Structural checks over the reusable workflows' step layout.

Both workflows check the caller's repository out under a subdirectory
(``src``), never at the workspace root, and check this action's own source
(``.sast-sonarqube``) and the fallback baseline checkout
(``.baseline-checkout``) into separate sibling directories instead. These
tests pin the invariants that layout depends on:

* the caller's own checkout (the first ``actions/checkout`` step in the job)
  lands under a subdirectory, not the workspace root - so this action's own
  source, checked out separately into a sibling directory, never shares a
  directory with the caller's content;
* neither sibling directory (``.sast-sonarqube`` or ``.baseline-checkout``)
  lies under any directory passed to the scanner as ``project-base-dir``,
  for every step that invokes the scanner (``uses: ./.sast-sonarqube``);
* every checkout into a fixed `path:` (``src``, ``.sast-sonarqube``,
  ``.baseline-checkout``) is immediately preceded by a step that removes
  that path, under the same `if:` condition - clearing each fixed checkout
  path before use, independent of what an earlier job on the same runner
  left behind there; and
* no `run:` step writes its intermediate files to a workspace-relative path -
  scratch files go under $RUNNER_TEMP instead.
"""
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
    "$GITHUB_STEP_SUMMARY",
    "${GITHUB_STEP_SUMMARY}",
    "/dev/null",
}
ALLOWED_TARGET_PREFIXES = ("$RUNNER_TEMP/", "${RUNNER_TEMP}/")

ARITHMETIC = re.compile(r"\(\(.*?\)\)")
REDIRECT = re.compile(r"\d?>>?(?!&)\s*([^\s;|&)]+)")
TEE = re.compile(r"\btee(?:\s+-a)?\s+([^\s;|&)]+)")
OUTPUT_FLAG = re.compile(r"--(?:out|removed-out|result-out|dest)[\s=]+([^\s;|&)]+)")


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


def _write_targets(script):
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    # Drop bash arithmetic `(( ... ))`, where `>` is a comparison.
    text = ARITHMETIC.sub("", "\n".join(lines))
    for pattern in (REDIRECT, TEE, OUTPUT_FLAG):
        for match in pattern.finditer(text):
            yield match.group(1).strip("\"'")


def _is_allowed_target(target):
    return target in ALLOWED_EXACT_TARGETS or target.startswith(ALLOWED_TARGET_PREFIXES)


def _lies_under(sibling, prefix_dir):
    """Whether `sibling` (a fixed, top-level directory name) lies under
    `prefix_dir` (the literal, non-templated portion of a project-base-dir
    value). Everything lies under the workspace root; otherwise, since every
    sibling directory lives directly at the workspace root, only a strict
    descendant relationship counts - `prefix_dir` being the sibling itself
    (e.g. scanning `.baseline-checkout` directly) is not "under"."""
    if prefix_dir in ("", ".", "./"):
        return True
    return sibling.startswith(prefix_dir.rstrip("/") + "/")


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_caller_checkout_is_not_at_workspace_root(workflow):
    found = False
    for job_name, steps in _steps(workflow):
        checkout_steps = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
        if not checkout_steps:
            continue
        found = True
        caller_checkout = checkout_steps[0]
        assert "repository" not in (caller_checkout.get("with") or {}), (
            f"{workflow}:{job_name}: expected the first checkout in the job to be "
            "the caller's own code (no `repository:` override)"
        )
        path = (caller_checkout.get("with") or {}).get("path")
        assert path, (
            f"{workflow}:{job_name}: caller checkout {caller_checkout.get('name')!r} "
            "has no `path:` - it checks out at the workspace root"
        )
        assert path not in (".", "./", ""), (
            f"{workflow}:{job_name}: caller checkout path {path!r} is the workspace root"
        )
    assert found, f"{workflow}: expected at least one checkout step"


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
