from fetch_findings import normalize


def test_normalize_strips_project_key_prefix_from_component():
    issue = {"component": "myproj:src/app.py", "rule": "python:S1", "severity": "MAJOR"}
    result = normalize(issue, "myproj")
    assert result["path"] == "src/app.py"


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
    assert result["severity"] is None
    assert result["message"] == ""
    assert result["impacts"] == []
    assert result["line"] is None
