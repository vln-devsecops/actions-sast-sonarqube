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


def test_render_body_always_contains_the_marker():
    body = render_body(result(), "tier a: artifact for merge-base abc")
    assert MARKER in body


def test_render_body_no_findings_shows_checkmark():
    body = render_body(result(total_new_findings=0), "fallback")
    assert "No new findings introduced by this PR" in body


def test_render_body_blocked_shows_blocked_message():
    body = render_body(result(blocked=True, total_new_findings=1), "tier b: artifact for target branch HEAD abc")
    assert "**Blocked**" in body
    assert "MAJOR" in body


def test_render_body_findings_but_not_blocked():
    body = render_body(result(blocked=False, total_new_findings=2), "fallback")
    assert "Not blocked" in body


def test_render_body_includes_baseline_tier():
    body = render_body(result(), "tier a: artifact for merge-base deadbeef")
    assert "tier a: artifact for merge-base deadbeef" in body


def test_render_body_includes_check_run_link_when_present():
    body = render_body(result(check_run_url="https://example.com/run/1"), "fallback")
    assert "https://example.com/run/1" in body


def test_render_body_omits_check_run_link_when_absent():
    body = render_body(result(check_run_url=None), "fallback")
    assert "View annotated findings" not in body


def test_render_body_shows_blocking_disabled():
    body = render_body(result(blocking_enabled=False), "fallback")
    assert "disabled" in body
