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


def severity_counts(**overrides):
    counts = {"BLOCKER": 0, "CRITICAL": 0, "MAJOR": 0, "MINOR": 0, "INFO": 0}
    counts.update(overrides)
    return counts


def test_render_body_always_contains_the_marker():
    body = render_body(result(), "tier a: artifact for merge-base abc", severity_counts(), severity_counts())
    assert MARKER in body


def test_render_body_no_findings_shows_checkmark():
    body = render_body(result(total_new_findings=0), "fallback", severity_counts(), severity_counts())
    assert "No new findings introduced by this PR" in body


def test_render_body_blocked_shows_blocked_message():
    body = render_body(
        result(blocked=True, total_new_findings=1), "tier b: artifact for target branch HEAD abc",
        severity_counts(), severity_counts(),
    )
    assert "**Blocked**" in body
    assert "MAJOR" in body


def test_render_body_findings_but_not_blocked():
    body = render_body(result(blocked=False, total_new_findings=2), "fallback", severity_counts(), severity_counts())
    assert "Not blocked" in body


def test_render_body_includes_baseline_tier():
    body = render_body(result(), "tier a: artifact for merge-base deadbeef", severity_counts(), severity_counts())
    assert "tier a: artifact for merge-base deadbeef" in body


def test_render_body_includes_check_run_link_when_present():
    body = render_body(
        result(check_run_url="https://example.com/run/1"), "fallback", severity_counts(), severity_counts(),
    )
    assert "https://example.com/run/1" in body


def test_render_body_omits_check_run_link_when_absent():
    body = render_body(result(check_run_url=None), "fallback", severity_counts(), severity_counts())
    assert "View annotated findings" not in body


def test_render_body_shows_blocking_disabled():
    body = render_body(result(blocking_enabled=False), "fallback", severity_counts(), severity_counts())
    assert "disabled" in body


def test_render_body_single_table_has_baseline_added_removed_columns():
    body = render_body(result(), "fallback", severity_counts(), severity_counts())
    assert "| Severity | Baseline | Added | Removed |" in body


def test_render_body_table_row_combines_all_three_counts_per_severity():
    """The whole point of one merged table: a reviewer can read a single row
    and see the baseline count, what this PR added, and what it resolved for
    that severity - not three numbers scattered across separate tables."""
    body = render_body(
        result(blocked=True, total_new_findings=1),
        "fallback",
        severity_counts(CRITICAL=7),
        severity_counts(CRITICAL=2),
    )
    # CRITICAL: baseline=7 (from baseline_counts), added=1 (from result's
    # by_severity, set by the `blocked=True` helper above), removed=2
    assert "| CRITICAL | 7 | 1 | 2 |" in body


def test_render_body_baseline_and_removed_are_independent_of_added():
    body = render_body(result(total_new_findings=0), "fallback", severity_counts(MAJOR=3), severity_counts(MAJOR=5))
    assert "| MAJOR | 3 | 0 | 5 |" in body


def test_render_body_only_one_findings_table():
    """Regression guard against reverting to the old two-table layout."""
    body = render_body(result(), "fallback", severity_counts(), severity_counts())
    assert body.count("| Severity |") == 1
