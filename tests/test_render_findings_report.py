from render_findings_report import render_report


def make_finding(rule="python:S1", path="a.py", line=10, severity="MAJOR", message="bad", type="ISSUE", **extra):
    return {"rule": rule, "path": path, "line": line, "severity": severity, "message": message, "type": type, **extra}


def test_render_report_no_findings_says_so():
    report = render_report([], "myproj")
    assert "No findings." in report
    assert "|" not in report  # no empty table when there's nothing to show


def test_render_report_includes_project_key_in_heading():
    report = render_report([], "myproj")
    assert "`myproj`" in report


def test_render_report_includes_finding_count():
    report = render_report([make_finding(), make_finding(rule="python:S2")], "p")
    assert "2 finding(s)." in report


def test_render_report_shows_exact_location():
    report = render_report([make_finding(path="src/app.py", line=42)], "p")
    assert "`src/app.py:42`" in report


def test_render_report_omits_trailing_colon_when_line_is_missing():
    report = render_report([make_finding(path="src/app.py", line=None)], "p")
    assert "`src/app.py`" in report
    assert "`src/app.py:`" not in report


def test_render_report_labels_hotspots_distinctly_from_issues():
    report = render_report([make_finding(type="SECURITY_HOTSPOT")], "p")
    assert "| Hotspot |" in report

    report = render_report([make_finding(type="ISSUE")], "p")
    assert "| Issue |" in report


def test_render_report_escapes_pipes_in_message_so_the_table_does_not_break():
    report = render_report([make_finding(message="a | b")], "p")
    assert "a \\| b" in report


def test_render_report_collapses_newlines_in_message():
    report = render_report([make_finding(message="line one\nline two")], "p")
    assert "line one line two" in report
    # No raw newline should have leaked into the row and split the table.
    for line in report.splitlines():
        assert "line one" not in line or "line two" in line


def test_render_report_sorts_worst_severity_first():
    findings = [
        make_finding(rule="python:S-info", severity="INFO"),
        make_finding(rule="python:S-blocker", severity="BLOCKER"),
        make_finding(rule="python:S-major", severity="MAJOR"),
    ]
    report = render_report(findings, "p")
    blocker_pos = report.index("python:S-blocker")
    major_pos = report.index("python:S-major")
    info_pos = report.index("python:S-info")
    assert blocker_pos < major_pos < info_pos


def test_render_report_sorts_unrecognized_severity_last_not_first():
    """A malformed/unknown severity must never sort above genuine
    BLOCKER/CRITICAL findings and bury them lower in a long report."""
    findings = [
        make_finding(rule="python:S-unknown", severity="NOT_A_REAL_SEVERITY"),
        make_finding(rule="python:S-blocker", severity="BLOCKER"),
    ]
    report = render_report(findings, "p")
    assert report.index("python:S-blocker") < report.index("python:S-unknown")


def test_render_report_handles_missing_fields_without_crashing():
    report = render_report([{}], "p")
    assert "| Issue |" in report
