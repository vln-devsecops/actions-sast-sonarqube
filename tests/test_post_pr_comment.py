from post_pr_comment import MARKER, render_body


def result(blocked=False, total_new_findings=0, threshold="MAJOR", blocking_enabled=True, check_run_url=None):
    return {
        "threshold": threshold,
        "blocking_enabled": blocking_enabled,
        "total_new_findings": total_new_findings,
        "blocking_findings": 1 if blocked else 0,
        "by_severity": {"BLOCKER": 0, "CRITICAL": 1 if blocked else 0, "MAJOR": 0, "MINOR": 0, "INFO": 0},
        "blocked": blocked,
        "check_run_url": check_run_url,
    }


def baseline_counts(**overrides):
    counts = {"BLOCKER": 0, "CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "INFO": 0}
    counts.update(overrides)
    return counts


def test_render_body_always_contains_the_marker():
    body = render_body(result(), "tier a: artifact for merge-base abc", baseline_counts())
    assert MARKER in body


def test_render_body_no_findings_shows_checkmark():
    body = render_body(result(total_new_findings=0), "fallback", baseline_counts())
    assert "No new findings introduced by this PR" in body


def test_render_body_blocked_shows_blocked_message():
    body = render_body(result(blocked=True, total_new_findings=1), "tier b: artifact for target branch HEAD abc", baseline_counts())
    assert "**Blocked**" in body
    assert "MAJOR" in body


def test_render_body_findings_but_not_blocked():
    body = render_body(result(blocked=False, total_new_findings=2), "fallback", baseline_counts())
    assert "Not blocked" in body


def test_render_body_includes_baseline_tier():
    body = render_body(result(), "tier a: artifact for merge-base deadbeef", baseline_counts())
    assert "tier a: artifact for merge-base deadbeef" in body


def test_render_body_includes_check_run_link_when_present():
    body = render_body(result(check_run_url="https://example.com/run/1"), "fallback", baseline_counts())
    assert "https://example.com/run/1" in body


def test_render_body_omits_check_run_link_when_absent():
    body = render_body(result(check_run_url=None), "fallback", baseline_counts())
    assert "View annotated findings" not in body


def test_render_body_shows_blocking_disabled():
    body = render_body(result(blocking_enabled=False), "fallback", baseline_counts())
    assert "disabled" in body


def test_render_body_shows_baseline_findings_table():
    """The whole point of this table: distinguish "0 new findings against a
    clean baseline" from "0 new findings against a baseline already carrying
    known defects" - the two used to render identically."""
    body = render_body(result(total_new_findings=0), "fallback", baseline_counts(MAJOR=3, MINOR=5))
    assert "**Baseline**" in body
    assert "8 finding(s) already present" in body
    assert "| MAJOR | 3 |" in body
    assert "| MINOR | 5 |" in body


def test_render_body_baseline_table_shows_zero_for_a_clean_baseline():
    body = render_body(result(total_new_findings=0), "fallback", baseline_counts())
    assert "0 finding(s) already present" in body
    assert "| BLOCKER | 0 |" in body


def test_render_body_baseline_table_is_independent_of_new_findings_table():
    """A finding present in both the new-findings breakdown and the baseline
    breakdown are unrelated counts of unrelated sets - confirm they don't get
    conflated when both are non-zero for the same severity."""
    body = render_body(result(blocked=True, total_new_findings=1), "fallback", baseline_counts(CRITICAL=7))
    new_findings_section, baseline_section = body.split("**Baseline**")
    assert "| CRITICAL | 1 |" in new_findings_section
    assert "| CRITICAL | 7 |" in baseline_section
