from pathlib import Path

import pytest
import yaml

from check_checkout_order import (
    find_checkout_order_violations,
    find_mismatched_recheckout_conditions,
    find_workspace_write_violations,
)

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


@pytest.mark.parametrize(
    "workspace_path",
    [
        "${{ github.workspace }}",
        "${{github.workspace}}",
        "${{  github.workspace  }}",
    ],
)
def test_github_workspace_path_counts_as_root(workspace_path):
    steps = [
        checkout(".sast-sonarqube"),
        checkout(workspace_path),  # root-equivalent - must wipe .sast-sonarqube too
        run_step("use it"),
    ]
    assert find_checkout_order_violations(steps) == ["use it"]


def test_dot_slash_action_source_path_is_a_trusted_checkout():
    """path: ./.sast-sonarqube is the same target as path: .sast-sonarqube -
    a caller/future edit spelling it with the leading ./ must still count."""
    steps = [checkout("./.sast-sonarqube"), run_step("use it")]
    assert find_checkout_order_violations(steps) == []


def test_dot_slash_action_source_path_restores_trust_after_a_wipe():
    steps = [
        checkout(".sast-sonarqube"),
        checkout(None),
        checkout("./.sast-sonarqube"),
        run_step("use it"),
    ]
    assert find_checkout_order_violations(steps) == []


def test_pure_removal_of_action_source_is_not_a_use():
    """The symlink-hardening step between a root checkout and the
    re-checkout (`rm -rf -- ./.sast-sonarqube`) unlinks the path without
    reading or executing anything under it - it must not itself be
    flagged as a "use" requiring a trusted checkout first, even though it
    runs in the gap where trust has just been revoked."""
    steps = [
        checkout(".sast-sonarqube"),
        checkout(None),  # root - wipes it
        {"name": "remove", "run": "rm -rf -- ./.sast-sonarqube"},
        checkout(".sast-sonarqube"),  # re-checkout restores it
    ]
    assert find_checkout_order_violations(steps) == []


def test_removal_variants_without_the_double_dash_or_leading_dot_slash():
    for text in ["rm -rf .sast-sonarqube", "rm -rf ./.sast-sonarqube", "rm -rf -- .sast-sonarqube"]:
        steps = [checkout(None), {"name": "remove", "run": text}]
        assert find_checkout_order_violations(steps) == [], text


def test_removal_combined_with_something_else_is_still_a_use():
    """Only a PURE removal is exempt - if a step also does anything else
    with the path, the broad substring rule still applies."""
    steps = [
        checkout(None),
        {"name": "remove and read", "run": "rm -rf -- ./.sast-sonarqube && cat .sast-sonarqube/scripts/foo.py"},
    ]
    assert find_checkout_order_violations(steps) == ["remove and read"]


@pytest.mark.parametrize(
    "step",
    [
        {"name": "working dir", "working-directory": ".sast-sonarqube", "run": "./some-script.sh"},
        {"name": "with value", "uses": "some/action@v1", "with": {"path": ".sast-sonarqube/fixture"}},
        {"name": "env value", "run": "echo hi", "env": {"SCRIPTS": ".sast-sonarqube/scripts"}},
    ],
)
def test_action_source_reference_anywhere_in_the_step_counts_as_a_use(step):
    """Not just run:/uses: - working-directory:, any with: value, and any
    env: value can all reference .sast-sonarqube too."""
    steps = [checkout(None), step]  # root checkout wipes it, nothing restores it
    violations = find_checkout_order_violations(steps)
    assert violations == ["working dir"] or violations == ["with value"] or violations == ["env value"]


def test_action_source_reference_inside_a_list_value_counts_as_a_use():
    """_all_string_values() walks lists too, not just nested dicts - not a
    shape any real `with:`/`env:` entry in this repo uses today, but the
    scan is written to be correct about it regardless."""
    steps = [
        checkout(None),
        {"name": "list value", "uses": "some/action@v1", "with": {"paths": [".sast-sonarqube/fixture", "other"]}},
    ]
    assert find_checkout_order_violations(steps) == ["list value"]


# --- find_mismatched_recheckout_conditions() -----------------------------------

def test_identical_if_conditions_on_checkout_and_recheckout_pass():
    steps = [
        {**checkout(None), "if": "contains(fromJSON('[\"a\"]'), x)"},
        {**checkout(".sast-sonarqube"), "if": "contains(fromJSON('[\"a\"]'), x)"},
    ]
    assert find_mismatched_recheckout_conditions(steps) == []


def test_mismatched_if_conditions_on_checkout_and_recheckout_are_flagged():
    steps = [
        {**checkout(None), "name": "root checkout", "if": "x == 'a'"},
        {**checkout(".sast-sonarqube"), "name": "recheckout", "if": "x == 'b'"},
    ]
    violations = find_mismatched_recheckout_conditions(steps)
    assert violations == [("root checkout", "recheckout")]


def test_both_missing_if_conditions_is_not_a_mismatch():
    steps = [checkout(None), checkout(".sast-sonarqube")]
    assert find_mismatched_recheckout_conditions(steps) == []


def test_one_missing_if_condition_is_a_mismatch():
    steps = [
        {**checkout(None), "name": "root"},
        {**checkout(".sast-sonarqube"), "name": "recheckout", "if": "x == 'a'"},
    ]
    assert find_mismatched_recheckout_conditions(steps) == [("root", "recheckout")]


def test_matching_conditions_with_a_non_checkout_step_between_are_fine():
    """A non-checkout step between the two (e.g. a cleanup step removing a
    symlink before the re-checkout) must not prevent the comparison - the
    real workflow has exactly this shape."""
    steps = [
        {**checkout(None), "if": "x == 'a'"},
        run_step("cleanup in between"),
        {**checkout(".sast-sonarqube"), "if": "x == 'a'"},
    ]
    assert find_mismatched_recheckout_conditions(steps) == []


def test_mismatched_conditions_are_still_flagged_with_a_non_checkout_step_between():
    steps = [
        {**checkout(None), "name": "root", "if": "x == 'a'"},
        run_step("cleanup in between"),
        {**checkout(".sast-sonarqube"), "name": "recheckout", "if": "x == 'b'"},
    ]
    assert find_mismatched_recheckout_conditions(steps) == [("root", "recheckout")]


def test_another_checkout_between_breaks_the_pairing():
    """A checkout to some OTHER path between the root checkout and the
    next action-source checkout means this isn't the direct
    "wipe, then immediately re-checkout" fix pattern - give up without
    comparing anything, even though the conditions here do differ."""
    steps = [
        {**checkout(None), "if": "x == 'a'"},
        {**checkout(".baseline-checkout"), "if": "x == 'a'"},
        {**checkout(".sast-sonarqube"), "if": "x == 'b'"},
    ]
    assert find_mismatched_recheckout_conditions(steps) == []


# --- find_workspace_write_violations() ------------------------------------------

def test_redirect_to_a_bare_relative_path_is_a_violation():
    steps = [{"name": "write", "run": 'echo hi > changed-files.txt'}]
    assert find_workspace_write_violations(steps) == [("write", "changed-files.txt")]


def test_append_redirect_to_a_bare_relative_path_is_a_violation():
    steps = [{"name": "write", "run": 'echo hi >> changed-files.txt'}]
    assert find_workspace_write_violations(steps) == [("write", "changed-files.txt")]


@pytest.mark.parametrize("flag", ["--out", "--dest", "--result-out", "--removed-out"])
def test_write_flags_to_a_bare_relative_path_are_violations(flag):
    steps = [{"name": "write", "run": f"python3 foo.py {flag} new-findings.json"}]
    assert find_workspace_write_violations(steps) == [("write", "new-findings.json")]


def test_quoted_bare_relative_path_is_still_a_violation():
    steps = [{"name": "write", "run": 'python3 foo.py --out "policy-result.json"'}]
    assert find_workspace_write_violations(steps) == [("write", "policy-result.json")]


def test_runner_temp_prefixed_target_is_safe():
    steps = [{"name": "write", "run": 'echo hi > "$RUNNER_TEMP/changed-files.txt"'}]
    assert find_workspace_write_violations(steps) == []


def test_runner_temp_write_flag_target_is_safe():
    steps = [{"name": "write", "run": 'python3 foo.py --dest "$RUNNER_TEMP/baseline-artifact"'}]
    assert find_workspace_write_violations(steps) == []


def test_absolute_path_target_is_safe():
    steps = [{"name": "write", "run": "echo hi > /tmp/scratch.txt"}]
    assert find_workspace_write_violations(steps) == []


def test_github_output_and_step_summary_appends_are_safe():
    steps = [{"name": "write", "run": 'echo "x=1" >> "$GITHUB_OUTPUT"\necho "y" >> "$GITHUB_STEP_SUMMARY"'}]
    assert find_workspace_write_violations(steps) == []


def test_dev_null_target_is_safe():
    steps = [{"name": "write", "run": "git rev-parse --verify origin/main >/dev/null 2>&1"}]
    assert find_workspace_write_violations(steps) == []


def test_local_variable_resolving_to_a_safe_runner_temp_path_is_safe():
    steps = [
        {
            "name": "write",
            "run": 'pr_context_file="$RUNNER_TEMP/pr-context.json"\npython3 foo.py --out "$pr_context_file"',
        }
    ]
    assert find_workspace_write_violations(steps) == []


def test_local_variable_resolving_to_an_unsafe_bare_path_is_still_flagged():
    steps = [{"name": "write", "run": 'f="changed-files.txt"\necho hi > "$f"'}]
    assert find_workspace_write_violations(steps) == [("write", "changed-files.txt")]


def test_unresolvable_variable_reference_is_treated_as_unsafe():
    """A $VAR this step never assigns (e.g. one only set via env:, outside
    this run: block) can't be proven safe - the check stays cautious."""
    steps = [{"name": "write", "run": 'echo hi > "$SOME_ENV_VAR"'}]
    assert find_workspace_write_violations(steps) == [("write", "$SOME_ENV_VAR")]


def test_braced_variable_reference_resolves_too():
    steps = [{"name": "write", "run": 'f="$RUNNER_TEMP/x.json"\necho hi > "${f}"'}]
    assert find_workspace_write_violations(steps) == []


def test_arithmetic_comparison_is_not_mistaken_for_a_redirect():
    """`(( count > 1 ))` matches the same ">" shape a redirect would - a
    bare numeric target is never a real intermediate file in this
    codebase's conventions, so it must not be flagged."""
    steps = [{"name": "write", "run": "if (( count > 1 )); then\n  echo many\nfi"}]
    assert find_workspace_write_violations(steps) == []


def test_stderr_fd_duplication_is_not_mistaken_for_a_write_target():
    steps = [{"name": "write", "run": "some-command 2>&1 | tee out.log"}]
    # 2>&1 must not be misread as redirecting to a bare file named "&1" -
    # and "tee out.log" is a positional arg, not a `>`/`--out` redirect, so
    # it's intentionally not inspected here at all.
    assert find_workspace_write_violations(steps) == []


def test_multiple_violations_in_one_step_are_all_reported():
    steps = [{"name": "write", "run": "echo a > one.txt\necho b > two.txt"}]
    violations = find_workspace_write_violations(steps)
    assert sorted(violations) == [("write", "one.txt"), ("write", "two.txt")]


def test_step_with_no_run_has_no_workspace_write_violations():
    steps = [{"name": "noop", "uses": "actions/checkout@v7"}]
    assert find_workspace_write_violations(steps) == []


def test_step_with_a_non_string_run_value_is_skipped():
    """Defensive: `run:` is always a string in valid workflow YAML, but a
    malformed/non-string value must not crash this check."""
    steps = [{"name": "odd", "run": None}]
    assert find_workspace_write_violations(steps) == []


def test_local_variable_resolving_to_an_empty_string_is_treated_as_safe():
    """An edge case with no real security weight (an empty redirect target
    is a shell error at runtime, not a hostile-tree write primitive) -
    pinned so _is_safe_write_target's early return is exercised directly
    through the public function, not just by inspection."""
    steps = [{"name": "write", "run": 'f=""\necho hi > "$f"'}]
    assert find_workspace_write_violations(steps) == []


def test_sonar_pr_yml_pr_scan_job_has_no_workspace_write_violations():
    """The real-file regression test: every intermediate file a trusted
    step writes must live under $RUNNER_TEMP, not a bare workspace-root
    path a hostile PR tree could have pre-committed as a symlink."""
    steps = _load_job_steps(".github/workflows/sonar-pr.yml", "pr-scan")
    assert find_workspace_write_violations(steps) == []


def test_sonar_pr_yml_pr_scan_job_has_no_mismatched_recheckout_conditions():
    steps = _load_job_steps(".github/workflows/sonar-pr.yml", "pr-scan")
    assert find_mismatched_recheckout_conditions(steps) == []


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
