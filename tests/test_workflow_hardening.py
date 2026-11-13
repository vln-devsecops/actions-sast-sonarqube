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
  for every step that invokes the scanner (``uses: ./.sast-sonarqube``); and
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
