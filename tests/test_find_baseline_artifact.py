from find_baseline_artifact import select_artifact


def artifact(name="sonar-baseline-abc123", expired=False, created_at="2026-01-01T00:00:00Z", **extra):
    return {"name": name, "expired": expired, "created_at": created_at, **extra}


def test_select_artifact_returns_none_when_no_artifacts():
    assert select_artifact([], "sonar-baseline-abc123") is None


def test_select_artifact_ignores_non_matching_names():
    artifacts = [artifact(name="sonar-baseline-other")]
    assert select_artifact(artifacts, "sonar-baseline-abc123") is None


def test_select_artifact_ignores_expired_artifacts():
    artifacts = [artifact(expired=True)]
    assert select_artifact(artifacts, "sonar-baseline-abc123") is None


def test_select_artifact_picks_the_single_match():
    artifacts = [artifact()]
    assert select_artifact(artifacts, "sonar-baseline-abc123") == artifacts[0]


def test_select_artifact_prefers_newest_on_collision():
    older = artifact(created_at="2026-01-01T00:00:00Z", id=1)
    newer = artifact(created_at="2026-06-01T00:00:00Z", id=2)
    result = select_artifact([older, newer], "sonar-baseline-abc123")
    assert result["id"] == 2


def test_select_artifact_skips_expired_even_if_only_candidate_with_matching_name():
    artifacts = [
        artifact(expired=True, id=1),
        artifact(name="sonar-baseline-other", expired=False, id=2),
    ]
    assert select_artifact(artifacts, "sonar-baseline-abc123") is None
