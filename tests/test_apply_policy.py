import json

import pytest

from apply_policy import (
    ANNOTATION_LEVEL,
    SEVERITY_ORDER,
    UNKNOWN_SEVERITY_FALLBACK,
    batch,
    build_summary,
    counts_by_severity,
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
