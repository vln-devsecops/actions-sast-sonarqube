import pytest

from fetch_findings import (
    UNKNOWN_SEVERITY_FALLBACK,
    _dedupe,
    normalize,
    normalize_hotspot,
    plan_partitions,
)


def test_normalize_strips_project_key_prefix_from_component():
    issue = {"component": "myproj:src/app.py", "rule": "python:S1", "severity": "MAJOR"}
    result = normalize(issue, "myproj")
    assert result["path"] == "src/app.py"


def test_normalize_tags_issues_with_type():
    issue = {"component": "p:a.py", "rule": "r", "severity": "MAJOR"}
    assert normalize(issue, "p")["type"] == "ISSUE"


def test_normalize_leaves_component_alone_if_prefix_does_not_match():
    issue = {"component": "otherproj:src/app.py", "rule": "python:S1", "severity": "MAJOR"}
    result = normalize(issue, "myproj")
    assert result["path"] == "otherproj:src/app.py"


def test_normalize_empty_hash_becomes_none():
    issue = {"component": "p:a.py", "hash": "", "rule": "r", "severity": "MINOR"}
    assert normalize(issue, "p")["hash"] is None


def test_normalize_preserves_hash_when_present():
    issue = {"component": "p:a.py", "hash": "abc123", "rule": "r", "severity": "MINOR"}
    assert normalize(issue, "p")["hash"] == "abc123"


def test_normalize_maps_impacts_list():
    issue = {
        "component": "p:a.py",
        "rule": "r",
        "severity": "MAJOR",
        "impacts": [{"softwareQuality": "SECURITY", "severity": "HIGH", "extraKeyIsIgnored": True}],
    }
    result = normalize(issue, "p")
    assert result["impacts"] == [{"softwareQuality": "SECURITY", "severity": "HIGH"}]


def test_normalize_defaults_missing_fields():
    issue = {"component": "p:a.py"}
    result = normalize(issue, "p")
    assert result["rule"] is None
    assert result["severity"] == UNKNOWN_SEVERITY_FALLBACK
    assert result["message"] == ""
    assert result["impacts"] == []
    assert result["line"] is None


def test_normalize_unrecognized_severity_fails_closed():
    """SonarQube's API contract doesn't guarantee `severity` is populated or
    recognized. A finding whose severity can't be determined must never
    become None - that crashed apply_policy.py in three places and, in the
    one path that didn't crash, silently dropped the finding from the
    reported counts."""
    issue = {"component": "p:a.py", "rule": "r", "severity": "NOT_A_REAL_SEVERITY"}
    assert normalize(issue, "p")["severity"] == UNKNOWN_SEVERITY_FALLBACK


def test_normalize_hotspot_strips_project_key_prefix_from_component():
    hotspot = {"component": "myproj:src/app.py", "ruleKey": "python:S4790", "vulnerabilityProbability": "MEDIUM"}
    result = normalize_hotspot(hotspot, "myproj")
    assert result["path"] == "src/app.py"


def test_normalize_hotspot_tags_type():
    hotspot = {"component": "p:a.py", "ruleKey": "r", "vulnerabilityProbability": "LOW"}
    assert normalize_hotspot(hotspot, "p")["type"] == "SECURITY_HOTSPOT"


def test_normalize_hotspot_uses_rule_key_not_rule():
    hotspot = {"component": "p:a.py", "ruleKey": "python:S4790", "vulnerabilityProbability": "LOW"}
    assert normalize_hotspot(hotspot, "p")["rule"] == "python:S4790"


def test_normalize_hotspot_never_has_a_hash():
    hotspot = {"component": "p:a.py", "ruleKey": "r", "vulnerabilityProbability": "HIGH"}
    assert normalize_hotspot(hotspot, "p")["hash"] is None


def test_normalize_hotspot_has_no_impacts():
    hotspot = {"component": "p:a.py", "ruleKey": "r", "vulnerabilityProbability": "HIGH"}
    assert normalize_hotspot(hotspot, "p")["impacts"] == []


@pytest.mark.parametrize(
    "probability,severity",
    [("HIGH", "CRITICAL"), ("MEDIUM", "MAJOR"), ("LOW", "MINOR")],
)
def test_normalize_hotspot_maps_vulnerability_probability_to_severity(probability, severity):
    hotspot = {"component": "p:a.py", "ruleKey": "r", "vulnerabilityProbability": probability}
    assert normalize_hotspot(hotspot, "p")["severity"] == severity


def test_normalize_hotspot_defaults_missing_fields():
    hotspot = {"component": "p:a.py"}
    result = normalize_hotspot(hotspot, "p")
    assert result["rule"] is None
    assert result["severity"] == UNKNOWN_SEVERITY_FALLBACK
    assert result["message"] == ""
    assert result["line"] is None


def test_normalize_hotspot_unknown_probability_fails_closed():
    """An unrecognized vulnerabilityProbability must not produce a null
    severity - see test_normalize_unrecognized_severity_fails_closed for why
    that matters."""
    hotspot = {"component": "p:a.py", "ruleKey": "r", "vulnerabilityProbability": "BRAND_NEW"}
    assert normalize_hotspot(hotspot, "p")["severity"] == UNKNOWN_SEVERITY_FALLBACK


def test_plan_partitions_none_when_paging_got_everything():
    assert plan_partitions(total=300, fetched=300, partitions=(("severities", ("A", "B")),)) is None


def test_plan_partitions_none_when_fetched_exceeds_total():
    """Defensive: total is a point-in-time snapshot from page 1 and could in
    principle be stale by the last page if issues are created mid-scan."""
    assert plan_partitions(total=300, fetched=305, partitions=(("severities", ("A", "B")),)) is None


def test_plan_partitions_splits_along_first_dimension_when_short():
    plan = plan_partitions(
        total=12000, fetched=10000,
        partitions=(("severities", ("INFO", "MINOR")), ("types", ("BUG",))),
    )
    assert plan == ("severities", ("INFO", "MINOR"), (("types", ("BUG",)),))


def test_plan_partitions_empty_values_when_no_dimension_left():
    plan = plan_partitions(total=12000, fetched=10000, partitions=())
    assert plan == ("", (), ())


def test_dedupe_drops_findings_sharing_a_hash_key():
    finding = {"rule": "r", "path": "a.py", "hash": "abc", "line": 1}
    assert _dedupe([finding, dict(finding)]) == [finding]


def test_dedupe_drops_findings_sharing_a_line_key_when_hash_absent():
    finding = {"rule": "r", "path": "a.py", "hash": None, "line": 5}
    assert _dedupe([finding, dict(finding)]) == [finding]


def test_dedupe_keeps_distinct_findings():
    a = {"rule": "r1", "path": "a.py", "hash": "abc", "line": 1}
    b = {"rule": "r2", "path": "a.py", "hash": "def", "line": 2}
    assert _dedupe([a, b]) == [a, b]
