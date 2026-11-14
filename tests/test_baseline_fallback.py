import re
from pathlib import Path

import pytest

from baseline_fallback import BUILD_FAILURE_REASONS, is_build_failure_reason


@pytest.mark.parametrize(
    "reason",
    ["build-command-failed", "no-analyzable-projects", "project-not-built", "project-not-analyzed"],
)
def test_build_failure_reasons_are_classified_as_build_failures(reason):
    assert is_build_failure_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        "begin-failed",
        "end-failed",
        "scanner-install-failed",
        "no-report-task",
        "ce-task-failed",
        "ce-task-timeout",
        "invalid-sast-config",
        "build-command-required",
        "build-command-not-allowed",
        "invalid-scanner-mode",
        "project-base-dir-missing",
        "project-base-dir-outside-workspace",
        "workspace-inaccessible",
        "scanner-failed",
        "check-failed",
    ],
)
def test_infra_and_config_reasons_are_not_classified_as_build_failures(reason):
    """These must NOT masquerade as "the base didn't build" - doing so
    would hide an unrelated problem (e.g. SonarQube itself being down)
    behind an empty baseline that looks like a merely permissive choice.
    check-failed means check_dotnet_projects.py itself crashed before
    reaching a verdict - that's a broken CHECK, not evidence about whether
    the target branch's code builds, so it must fail closed (propagate as
    a real failure) rather than be read as "nothing to worry about"."""
    assert is_build_failure_reason(reason) is False


@pytest.mark.parametrize("reason", ["", None, "   ", "not-a-real-reason-code"])
def test_unknown_or_empty_reason_is_not_a_build_failure(reason):
    assert is_build_failure_reason(reason) is False


def test_every_build_failure_reason_is_a_real_run_scan_failure_reason_code():
    """Pin the exact set so a future run_scan.sh rename doesn't silently
    stop matching here."""
    assert BUILD_FAILURE_REASONS == {
        "build-command-failed",
        "no-analyzable-projects",
        "project-not-built",
        "project-not-analyzed",
    }


def test_every_run_scan_failure_reason_is_classified_in_the_module_comment():
    """Every code run_scan.sh can set is either a build-failure reason or
    named in baseline_fallback.py's "Deliberately NOT included" comment, so
    a new code (e.g. #55's workspace checks) is a conscious decision here,
    not an accident of the default."""
    root = Path(__file__).resolve().parent.parent
    reasons = set(re.findall(r'^\s*fail ([a-z][a-z-]+) "', (root / "scripts" / "run_scan.sh").read_text(), re.M))
    source = (root / "scripts" / "baseline_fallback.py").read_text()
    comment = source[source.index("# Reason codes from run_scan.sh") : source.index("BUILD_FAILURE_REASONS = ")]
    unclassified = sorted(r for r in reasons - BUILD_FAILURE_REASONS if r not in comment)
    assert unclassified == []
