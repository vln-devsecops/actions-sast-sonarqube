import http.server
import json
import threading

import pytest

import apply_policy
from apply_policy import (
    ANNOTATION_LEVEL,
    SEVERITY_ORDER,
    UNKNOWN_SEVERITY_FALLBACK,
    batch,
    build_summary,
    counts_by_severity,
    create_check_run,
    gh_api,
    main,
    severity_at_least,
    severity_of,
    to_annotation,
)


@pytest.mark.parametrize(
    "severity,threshold,expected",
    [
        ("MAJOR", "MAJOR", True),
        ("CRITICAL", "MAJOR", True),
        ("BLOCKER", "MAJOR", True),
        ("MINOR", "MAJOR", False),
        ("INFO", "MAJOR", False),
        ("INFO", "INFO", True),
        ("BLOCKER", "BLOCKER", True),
    ],
)
def test_severity_at_least(severity, threshold, expected):
    assert severity_at_least(severity, threshold) is expected


def test_severity_order_is_low_to_high():
    assert SEVERITY_ORDER == ["INFO", "MINOR", "MAJOR", "CRITICAL", "BLOCKER"]


def test_severity_at_least_fails_closed_on_unrecognized_severity():
    """fetch_findings.py guarantees a valid severity, but baseline artifacts
    are persisted JSON up to artifact-retention-days old and can predate that
    guarantee. An unrecognized severity used to raise ValueError here
    (SEVERITY_ORDER.index(None)) and crash the whole policy step."""
    assert severity_at_least(None, "MAJOR") is True
    assert severity_at_least("NOT_A_REAL_SEVERITY", "MAJOR") is True


def test_severity_of_returns_valid_severity_unchanged():
    assert severity_of({"severity": "MINOR"}) == "MINOR"


@pytest.mark.parametrize("bad_severity", [None, "", "NOT_A_REAL_SEVERITY"])
def test_severity_of_falls_back_on_unrecognized_severity(bad_severity):
    assert severity_of({"severity": bad_severity}) == UNKNOWN_SEVERITY_FALLBACK


def test_severity_of_falls_back_when_severity_key_missing():
    assert severity_of({}) == UNKNOWN_SEVERITY_FALLBACK


def test_to_annotation_shapes_finding_correctly():
    finding = {"path": "a.py", "line": 42, "rule": "python:S1", "severity": "CRITICAL", "message": "bad"}
    ann = to_annotation(finding)
    assert ann == {
        "path": "a.py",
        "start_line": 42,
        "end_line": 42,
        "annotation_level": "failure",
        "title": "python:S1 (CRITICAL)",
        "message": "bad",
    }


def test_to_annotation_prefixes_security_hotspots():
    finding = {
        "path": "a.py", "line": 1, "rule": "python:S4790", "severity": "MAJOR",
        "message": "", "type": "SECURITY_HOTSPOT",
    }
    assert to_annotation(finding)["title"] == "Security Hotspot: python:S4790 (MAJOR)"


def test_to_annotation_does_not_prefix_issues():
    finding = {"path": "a.py", "line": 1, "rule": "python:S1", "severity": "MAJOR", "message": "", "type": "ISSUE"}
    assert to_annotation(finding)["title"] == "python:S1 (MAJOR)"


def test_to_annotation_fails_closed_on_unrecognized_severity():
    """This used to be a bare KeyError from ANNOTATION_LEVEL[None]."""
    finding = {"path": "a.py", "line": 1, "rule": "r", "severity": None, "message": ""}
    ann = to_annotation(finding)
    assert ann["title"] == f"r ({UNKNOWN_SEVERITY_FALLBACK})"
    assert ann["annotation_level"] == ANNOTATION_LEVEL[UNKNOWN_SEVERITY_FALLBACK]


def test_to_annotation_defaults_missing_line_to_1():
    finding = {"path": "a.py", "line": None, "rule": "python:S1", "severity": "MINOR", "message": ""}
    ann = to_annotation(finding)
    assert ann["start_line"] == 1
    assert ann["end_line"] == 1


@pytest.mark.parametrize(
    "severity,level",
    [("BLOCKER", "failure"), ("CRITICAL", "failure"), ("MAJOR", "warning"), ("MINOR", "notice"), ("INFO", "notice")],
)
def test_to_annotation_level_matches_severity(severity, level):
    finding = {"path": "a.py", "line": 1, "rule": "r", "severity": severity, "message": ""}
    assert to_annotation(finding)["annotation_level"] == level


def test_counts_by_severity_includes_zero_counts_for_absent_severities():
    findings = [{"severity": "MAJOR"}, {"severity": "MAJOR"}, {"severity": "MINOR"}]
    counts = counts_by_severity(findings)
    assert counts == {"INFO": 0, "MINOR": 1, "MAJOR": 2, "CRITICAL": 0, "BLOCKER": 0}


def test_counts_by_severity_empty_input():
    assert counts_by_severity([]) == {s: 0 for s in SEVERITY_ORDER}


def test_counts_by_severity_never_silently_drops_an_unrecognized_severity():
    """This used to insert a None key instead of counting the finding under
    a known severity - json.dump(..., sort_keys=True) on that result then
    raised TypeError (can't order None against str), and even where
    serialization succeeded, post_pr_comment.render_body()'s fixed severity
    list never rendered the None-keyed count - the PR comment reported an
    all-zeros table while a real finding existed."""
    counts = counts_by_severity([{"severity": None}])
    assert None not in counts
    assert sum(counts.values()) == 1
    assert counts[UNKNOWN_SEVERITY_FALLBACK] == 1
    json.dumps(counts, sort_keys=True)  # must not raise


def test_build_summary_blocking_enabled_reports_count():
    findings = [{"severity": "CRITICAL"}, {"severity": "MINOR"}]
    blocking_findings = [findings[0]]
    summary = build_summary(findings, blocking_findings, "MAJOR", True)
    assert "2 new finding(s)" in summary
    assert "1 new finding(s) meet or exceed it" in summary
    assert "**MAJOR**" in summary


def test_build_summary_blocking_disabled_says_so():
    summary = build_summary([], [], "MAJOR", False)
    assert "Blocking is disabled" in summary


@pytest.mark.parametrize(
    "count,size,expected_lengths",
    [
        (0, 50, []),
        (10, 50, [10]),
        (50, 50, [50]),
        (51, 50, [50, 1]),
        (120, 50, [50, 50, 20]),
    ],
)
def test_batch_splits_into_chunks_of_at_most_size(count, size, expected_lengths):
    items = list(range(count))
    result = batch(items, size)
    assert [len(chunk) for chunk in result] == expected_lengths
    assert [x for chunk in result for x in chunk] == items


def test_main_writes_result_out_even_when_check_run_publication_fails(tmp_path, monkeypatch):
    """--result-out is documented as always written, even when publishing the
    Check Run itself fails (e.g. the read-only GITHUB_TOKEN a fork PR gets).
    That used to not hold: the write happened after create_check_run(), so an
    API failure there skipped it entirely, and a later `if: always()` step
    reading the file died with FileNotFoundError instead of the real error."""
    findings_path = tmp_path / "findings.json"
    result_path = tmp_path / "result.json"
    findings_path.write_text(json.dumps([{"rule": "r", "path": "a.py", "line": 1, "severity": "MINOR", "message": "m"}]))

    def boom(*args, **kwargs):
        raise SystemExit("GitHub API POST .../check-runs failed: HTTP 403")

    monkeypatch.setattr(apply_policy, "create_check_run", boom)
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_policy.py",
            "--findings", str(findings_path),
            "--repo", "o/r",
            "--sha", "abc123",
            "--result-out", str(result_path),
        ],
    )

    with pytest.raises(SystemExit):
        main()

    assert result_path.exists()
    result = json.loads(result_path.read_text())
    assert result["check_run_url"] is None
    assert result["total_new_findings"] == 1


def test_main_result_out_gets_the_check_run_url_on_success(tmp_path, monkeypatch):
    findings_path = tmp_path / "findings.json"
    result_path = tmp_path / "result.json"
    findings_path.write_text(json.dumps([]))

    monkeypatch.setattr(
        apply_policy, "create_check_run",
        lambda *a, **k: {"html_url": "https://example.com/run/1"},
    )
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_policy.py",
            "--findings", str(findings_path),
            "--repo", "o/r",
            "--sha", "abc123",
            "--result-out", str(result_path),
        ],
    )

    main()

    result = json.loads(result_path.read_text())
    assert result["check_run_url"] == "https://example.com/run/1"


def _local_http_server(status_code):
    """A real local HTTP server returning `status_code` for every request,
    used to exercise gh_api's actual urllib error handling rather than
    mocking it away."""
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _respond(self):
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"message": "denied"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _respond
        do_POST = _respond
        do_PATCH = _respond

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_gh_api_raises_on_403_by_default():
    """The ordinary case: a 403 is a genuine error and should stop the job,
    not be silently swallowed."""
    server = _local_http_server(403)
    try:
        with pytest.raises(SystemExit):
            gh_api("POST", f"http://127.0.0.1:{server.server_port}/", "token")
    finally:
        server.shutdown()


def test_gh_api_returns_none_on_403_when_forbidden_allowed():
    """GitHub downgrades GITHUB_TOKEN to read-only for a pull_request event
    from a fork, regardless of the permissions: block a workflow requests -
    so a write call 403ing there is expected, not a genuine error, and must
    degrade gracefully rather than crash the whole job."""
    server = _local_http_server(403)
    try:
        result = gh_api("POST", f"http://127.0.0.1:{server.server_port}/", "token", allow_forbidden=True)
    finally:
        server.shutdown()
    assert result is None


def test_gh_api_returns_none_on_404_when_forbidden_allowed():
    server = _local_http_server(404)
    try:
        result = gh_api("POST", f"http://127.0.0.1:{server.server_port}/", "token", allow_forbidden=True)
    finally:
        server.shutdown()
    assert result is None


def test_gh_api_still_raises_on_other_errors_when_forbidden_allowed():
    """allow_forbidden is specifically about 403/404 - a 500 is still a
    genuine failure and must not be swallowed."""
    server = _local_http_server(500)
    try:
        with pytest.raises(SystemExit):
            gh_api("POST", f"http://127.0.0.1:{server.server_port}/", "token", allow_forbidden=True)
    finally:
        server.shutdown()


def test_create_check_run_returns_none_when_creation_is_forbidden(monkeypatch):
    """A fork PR's read-only token means the creating POST 403s - gh_api
    already surfaces that as None (tested above); create_check_run must
    propagate it as None too rather than crashing on check_run["id"], and
    must not attempt any PATCH follow-up calls with nothing to patch."""
    patch_calls = []
    monkeypatch.setattr(
        apply_policy, "gh_api",
        lambda method, url, token, body=None, allow_forbidden=False: (
            patch_calls.append(method) if method == "PATCH" else None
        ),
    )
    result = create_check_run(
        "o/r", "token", "sha", "check-name", "success", "summary",
        [{"path": "a.py", "start_line": 1, "end_line": 1, "annotation_level": "notice", "title": "t", "message": "m"}],
    )
    assert result is None
    assert patch_calls == []


def test_main_handles_a_completely_unpublishable_check_run(tmp_path, monkeypatch):
    """End-to-end fork-PR simulation: create_check_run returns None (as it
    does when GitHub Actions downgrades GITHUB_TOKEN to read-only for a
    pull_request event from a fork). main() must still write --result-out
    with check_run_url: None instead of crashing on check_run.get(...)."""
    findings_path = tmp_path / "findings.json"
    result_path = tmp_path / "result.json"
    findings_path.write_text(json.dumps([{"rule": "r", "path": "a.py", "line": 1, "severity": "MAJOR", "message": "m"}]))

    monkeypatch.setattr(apply_policy, "create_check_run", lambda *a, **k: None)
    monkeypatch.setenv("GITHUB_TOKEN", "dummy")
    monkeypatch.setattr(
        "sys.argv",
        [
            "apply_policy.py",
            "--findings", str(findings_path),
            "--repo", "o/r",
            "--sha", "abc123",
            "--result-out", str(result_path),
        ],
    )

    with pytest.raises(SystemExit):
        main()  # still blocks per policy (MAJOR finding), even unpublished

    result = json.loads(result_path.read_text())
    assert result["check_run_url"] is None
    assert result["blocked"] is True
