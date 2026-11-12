from pathlib import Path

import yaml

from check_checkout_order import find_checkout_order_violations

REPO_ROOT = Path(__file__).resolve().parent.parent


def checkout(path=None):
    step = {"uses": "actions/checkout@v7"}
    if path is not None:
        step["with"] = {"path": path}
    return step


def run_step(name, text=".sast-sonarqube/scripts/foo.py"):
    return {"name": name, "run": text}


def composite_step(name):
    return {"name": name, "uses": "./.sast-sonarqube"}


# --- pure find_checkout_order_violations() over synthetic step lists ----------

def test_no_violation_when_action_source_checkout_precedes_use():
    steps = [
        checkout(".sast-sonarqube"),
        run_step("use it"),
    ]
    assert find_checkout_order_violations(steps) == []


def test_no_violations_when_nothing_uses_action_source():
    steps = [checkout(None), checkout(".sast-sonarqube")]
    assert find_checkout_order_violations(steps) == []


def test_violation_when_root_checkout_follows_action_source_checkout_unrestored():
    """The actual bug (9652c9e): action-source checkout, then a root
    checkout (no path:) that wipes it, then a step that still assumes it's
    there."""
    steps = [
        checkout(".sast-sonarqube"),
        checkout(None),  # root - wipes .sast-sonarqube
        run_step("use it after the wipe"),
    ]
    violations = find_checkout_order_violations(steps)
    assert violations == ["use it after the wipe"]


def test_root_checkout_before_any_action_source_checkout_is_fine():
    """sonar-baseline.yml's own order: root checkout happens FIRST (while
    the root has no .sast-sonarqube yet to wipe), the action-source
    checkout comes after and is never followed by another root checkout."""
    steps = [
        checkout(None),  # root - nothing to wipe yet
        checkout(".sast-sonarqube"),
        run_step("use it"),
    ]
    assert find_checkout_order_violations(steps) == []


def test_fix_pattern_recheckout_after_root_checkout_clears_the_violation():
    """The actual fix: re-run the action-source checkout immediately after
    the root checkout that wiped it, before anything uses it again."""
    steps = [
        checkout(".sast-sonarqube"),
        checkout(None),  # root - wipes .sast-sonarqube
        checkout(".sast-sonarqube"),  # re-checkout - restores it
        run_step("use it"),
    ]
    assert find_checkout_order_violations(steps) == []


def test_path_dot_counts_as_root():
    steps = [checkout(".sast-sonarqube"), checkout("."), run_step("use it")]
    assert find_checkout_order_violations(steps) == ["use it"]


def test_path_dot_slash_counts_as_root():
    steps = [checkout(".sast-sonarqube"), checkout("./"), run_step("use it")]
    assert find_checkout_order_violations(steps) == ["use it"]


def test_non_root_non_action_source_checkout_does_not_reset_trust():
    """A checkout into a THIRD subdirectory (e.g. .baseline-checkout) only
    ever deletes contents under that one subdirectory - it must not be
    mistaken for a root checkout and spuriously flip trust off."""
    steps = [
        checkout(".sast-sonarqube"),
        checkout(".baseline-checkout"),
        run_step("use it"),
    ]
    assert find_checkout_order_violations(steps) == []


def test_composite_action_use_is_detected_not_just_run_steps():
    steps = [checkout(".sast-sonarqube"), checkout(None), composite_step("scan")]
    assert find_checkout_order_violations(steps) == ["scan"]


def test_step_without_a_name_is_identified_by_index():
    steps = [
        checkout(".sast-sonarqube"),
        checkout(None),
        {"run": ".sast-sonarqube/scripts/foo.py"},
    ]
    violations = find_checkout_order_violations(steps)
    assert violations == ["step[2]"]


def test_multiple_violations_are_all_reported():
    steps = [
        checkout(".sast-sonarqube"),
        checkout(None),
        run_step("first"),
        run_step("second"),
    ]
    assert find_checkout_order_violations(steps) == ["first", "second"]


def test_empty_step_list_has_no_violations():
    assert find_checkout_order_violations([]) == []


def test_step_with_no_run_or_uses_is_ignored():
    steps = [checkout(".sast-sonarqube"), checkout(None), {"name": "noop", "env": {}}]
    assert find_checkout_order_violations(steps) == []


# --- real files: this is the structural regression test, not a toy fixture ----

def _load_job_steps(workflow_path, job_id):
    with open(REPO_ROOT / workflow_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    return doc["jobs"][job_id]["steps"]


def test_sonar_pr_yml_pr_scan_job_has_no_checkout_order_violations():
    steps = _load_job_steps(".github/workflows/sonar-pr.yml", "pr-scan")
    assert find_checkout_order_violations(steps) == []


def test_sonar_baseline_yml_baseline_scan_job_has_no_checkout_order_violations():
    steps = _load_job_steps(".github/workflows/sonar-baseline.yml", "baseline-scan")
    assert find_checkout_order_violations(steps) == []


def test_sonar_pr_yml_actually_contains_a_root_checkout_of_pr_head():
    """Sanity check on the fixture-building helpers above and on the real
    file: if "Checkout PR head" ever stops being a root checkout (e.g.
    someone adds `path:` to it), the violation this test suite guards
    against can no longer occur there, and the regression test above would
    pass for the wrong reason. Pin that it's still a root checkout today."""
    steps = _load_job_steps(".github/workflows/sonar-pr.yml", "pr-scan")
    names = [s.get("name") for s in steps]
    assert "Checkout PR head" in names
    pr_head_step = steps[names.index("Checkout PR head")]
    with_block = pr_head_step.get("with") or {}
    assert "path" not in with_block
