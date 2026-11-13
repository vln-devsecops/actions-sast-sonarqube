"""Structural checks over the reusable workflows' step layout.

Both workflows check the caller's repository out at the workspace root
before running any of this repo's own steps, so the workspace contents are
not under this repo's control. These tests pin two invariants:

* every checkout into a fixed `path:` is immediately preceded by a step that
  removes that path (and runs under the same condition), so the checkout
  always starts from a fresh directory; and
* no `run:` step writes its intermediate files to a workspace-relative path -
  scratch files go under $RUNNER_TEMP instead.
"""
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WORKFLOWS = ["sonar-pr.yml", "sonar-baseline.yml"]

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


def _write_targets(script):
    lines = [line for line in script.splitlines() if not line.lstrip().startswith("#")]
    # Drop bash arithmetic `(( ... ))`, where `>` is a comparison.
    text = ARITHMETIC.sub("", "\n".join(lines))
    for pattern in (REDIRECT, TEE, OUTPUT_FLAG):
        for match in pattern.finditer(text):
            yield match.group(1).strip("\"'")


def _is_allowed_target(target):
    return target in ALLOWED_EXACT_TARGETS or target.startswith(ALLOWED_TARGET_PREFIXES)


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
