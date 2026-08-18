import zipfile

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


def test_select_artifact_tolerates_missing_fields():
    """A listing entry missing `expired`, `name`, or `created_at` must not
    raise - select_artifact()'s whole contract is "pick one or return None"."""
    matching_but_bare = {"name": "sonar-baseline-abc123"}
    assert select_artifact([matching_but_bare], "sonar-baseline-abc123") == matching_but_bare
    assert select_artifact([{}], "sonar-baseline-abc123") is None


def test_zip_extraction_cannot_escape_the_destination_directory(tmp_path):
    """download_and_extract() relies on zipfile.extractall() sanitizing member
    names rather than validating them itself. That reads like a "Zip Slip"
    path-traversal bug and has been flagged in review on that basis, so pin
    the behaviour it actually depends on: traversal and absolute-path members
    must both stay underneath the destination directory.

    If this ever fails, download_and_extract() is genuinely vulnerable and
    needs its own per-member validation - it is not a test-only concern.
    """
    archive = tmp_path / "evil.zip"
    dest = tmp_path / "dest"
    escaped_marker = tmp_path / "ESCAPED"

    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../ESCAPED", "pwned")
        zf.writestr("/absolute/ESCAPED", "pwned")
        zf.writestr("legit.json", "[]")

    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dest)

    assert not escaped_marker.exists(), "archive member escaped the destination directory"
    extracted = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    assert extracted == {"ESCAPED", "absolute/ESCAPED", "legit.json"}
    for path in dest.rglob("*"):
        assert dest in path.parents or path.parent == dest
