import json

from diff_findings import diff, filter_by_changed_files, finding_key, main


def make_finding(rule="python:S1", path="a.py", hash="h1", line=10, **extra):
    return {"rule": rule, "path": path, "hash": hash, "line": line, **extra}


def test_finding_key_uses_hash_when_present():
    f = make_finding(hash="abc", line=10)
    assert finding_key(f) == ("hash", "python:S1", "a.py", "abc")


def test_finding_key_falls_back_to_line_when_hash_empty():
    f = make_finding(hash=None, line=10)
    assert finding_key(f) == ("line", "python:S1", "a.py", 10)

    f2 = make_finding(hash="", line=10)
    assert finding_key(f2) == ("line", "python:S1", "a.py", 10)


def test_diff_excludes_finding_matched_by_hash_even_if_line_shifted():
    baseline = [make_finding(hash="h1", line=10)]
    head = [make_finding(hash="h1", line=25)]  # same hash, different line
    assert diff(baseline, head) == []


def test_diff_excludes_finding_matched_by_line_when_hash_empty():
    baseline = [make_finding(hash=None, line=5)]
    head = [make_finding(hash=None, line=5)]
    assert diff(baseline, head) == []


def test_diff_includes_genuinely_new_finding():
    baseline = [make_finding(rule="python:S1", path="a.py", hash="h1")]
    head = [
        make_finding(rule="python:S1", path="a.py", hash="h1"),
        make_finding(rule="python:S2", path="a.py", hash="h3"),
    ]
    result = diff(baseline, head)
    assert len(result) == 1
    assert result[0]["rule"] == "python:S2"


def test_diff_treats_different_hash_as_new_even_if_line_matches():
    baseline = [make_finding(hash="h1", line=10)]
    head = [make_finding(hash="h2", line=10)]
    assert diff(baseline, head) == [make_finding(hash="h2", line=10)]


def test_diff_hash_and_line_fallback_keys_do_not_collide():
    # A hash-bearing baseline finding must not accidentally match a
    # hash-less head finding just because they share rule/path/line.
    baseline = [make_finding(hash="h1", line=10)]
    head = [make_finding(hash=None, line=10)]
    assert diff(baseline, head) == head


def test_diff_empty_baseline_means_everything_is_new():
    head = [make_finding(), make_finding(rule="python:S2")]
    assert diff([], head) == head


def test_filter_by_changed_files_keeps_only_touched_paths():
    findings = [
        make_finding(path="python/app.py"),
        make_finding(path="js/app.js"),
    ]
    result = filter_by_changed_files(findings, {"python/app.py"})
    assert [f["path"] for f in result] == ["python/app.py"]


def test_filter_by_changed_files_drops_everything_on_path_space_mismatch():
    # Regression test for the bug where git-diff (repo-root-relative) paths
    # were compared against project-base-dir-relative finding paths.
    findings = [make_finding(path="python/app.py")]
    result = filter_by_changed_files(findings, {"fixtures/python/app.py"})
    assert result == []


def test_main_writes_new_findings_to_out_file(tmp_path, monkeypatch, capsys):
    baseline_path = tmp_path / "baseline.json"
    head_path = tmp_path / "head.json"
    out_path = tmp_path / "new.json"

    baseline_path.write_text(json.dumps([make_finding(hash="h1")]))
    head_path.write_text(
        json.dumps([make_finding(hash="h1"), make_finding(rule="python:S2", hash="h2")])
    )

    monkeypatch.setattr(
        "sys.argv",
        ["diff_findings.py", "--baseline", str(baseline_path), "--head", str(head_path), "--out", str(out_path)],
    )
    main()

    result = json.loads(out_path.read_text())
    assert len(result) == 1
    assert result[0]["rule"] == "python:S2"

    captured = capsys.readouterr()
    assert "1 new finding(s) out of 2 head finding(s)" in captured.out


def test_main_applies_changed_files_filter_end_to_end(tmp_path, monkeypatch):
    baseline_path = tmp_path / "baseline.json"
    head_path = tmp_path / "head.json"
    out_path = tmp_path / "new.json"
    changed_files_path = tmp_path / "changed-files.txt"

    baseline_path.write_text(json.dumps([]))
    head_path.write_text(
        json.dumps([make_finding(path="python/app.py"), make_finding(path="js/app.js")])
    )
    changed_files_path.write_text("python/app.py\n")

    monkeypatch.setattr(
        "sys.argv",
        [
            "diff_findings.py",
            "--baseline", str(baseline_path),
            "--head", str(head_path),
            "--out", str(out_path),
            "--changed-files", str(changed_files_path),
        ],
    )
    main()

    result = json.loads(out_path.read_text())
    assert [f["path"] for f in result] == ["python/app.py"]
