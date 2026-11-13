import os

import pytest

from build_scan_config import (
    build_scan_properties,
    parse_sastignore,
    parse_sastrc,
    translate_gitignore_pattern,
)


# --- parse_sastrc -----------------------------------------------------


def test_parse_sastrc_empty_text_is_empty_config():
    result = parse_sastrc("")
    assert result == {
        "exclusions": [],
        "coverage_exclusions": [],
        "duplication_exclusions": [],
        "tests": [],
        "ignore": [],
    }


def test_parse_sastrc_blank_yaml_is_empty_config():
    assert parse_sastrc("   \n") == parse_sastrc("")


def test_parse_sastrc_exclusions_map_correctly():
    text = """
exclusions:
  paths: ["**/vendor/**", "**/*.generated.go"]
  coverage_paths: ["**/mocks/**"]
  duplication_paths: ["**/testdata/**"]
"""
    result = parse_sastrc(text)
    assert result["exclusions"] == ["**/vendor/**", "**/*.generated.go"]
    assert result["coverage_exclusions"] == ["**/mocks/**"]
    assert result["duplication_exclusions"] == ["**/testdata/**"]


def test_parse_sastrc_tests_maps_correctly():
    text = """
tests:
  paths: ["src/**/*.test.ts", "features/**"]
"""
    result = parse_sastrc(text)
    assert result["tests"] == ["src/**/*.test.ts", "features/**"]


def test_parse_sastrc_rejects_unknown_tests_key():
    with pytest.raises(ValueError, match="unrecognized key"):
        parse_sastrc('tests:\n  sonar.host.url: "http://evil"\n')


def test_parse_sastrc_rejects_non_mapping_tests():
    with pytest.raises(ValueError, match="'tests' must be a mapping"):
        parse_sastrc('tests: ["src/**/*.test.ts"]\n')


def test_parse_sastrc_rejects_non_list_tests_paths():
    with pytest.raises(ValueError, match="must be a list of non-empty strings"):
        parse_sastrc('tests:\n  paths: "src/**/*.test.ts"\n')


def test_parse_sastrc_ignore_single_path_per_entry():
    text = """
ignore:
  - rule: "python:S101"
    paths: ["tests/**"]
"""
    result = parse_sastrc(text)
    assert result["ignore"] == [("python:S101", "tests/**")]


def test_parse_sastrc_ignore_fans_out_multiple_paths_per_entry():
    text = """
ignore:
  - rule: "*"
    paths: ["legacy/**", "vendor/**"]
"""
    result = parse_sastrc(text)
    assert result["ignore"] == [("*", "legacy/**"), ("*", "vendor/**")]


def test_parse_sastrc_rejects_unknown_top_level_key():
    with pytest.raises(ValueError, match="unrecognized top-level key"):
        parse_sastrc("quality_profile: foo\n")


def test_parse_sastrc_rejects_unknown_exclusions_key():
    with pytest.raises(ValueError, match="unrecognized key"):
        parse_sastrc("exclusions:\n  sonar.host.url: http://evil\n")


def test_parse_sastrc_rejects_unknown_ignore_key():
    with pytest.raises(ValueError, match="unrecognized key"):
        parse_sastrc('ignore:\n  - rule: "*"\n    paths: ["a/**"]\n    severity: BLOCKER\n')


def test_parse_sastrc_rejects_ignore_entry_missing_rule():
    with pytest.raises(ValueError, match="missing a non-empty 'rule'"):
        parse_sastrc('ignore:\n  - paths: ["a/**"]\n')


def test_parse_sastrc_rejects_ignore_entry_missing_paths():
    with pytest.raises(ValueError, match="missing 'paths'"):
        parse_sastrc('ignore:\n  - rule: "python:S101"\n')


def test_parse_sastrc_rejects_ignore_entry_with_empty_paths():
    with pytest.raises(ValueError, match="must not be empty"):
        parse_sastrc('ignore:\n  - rule: "python:S101"\n    paths: []\n')


def test_parse_sastrc_rejects_non_list_paths():
    with pytest.raises(ValueError, match="must be a list of non-empty strings"):
        parse_sastrc("exclusions:\n  paths: \"**/vendor/**\"\n")


def test_parse_sastrc_null_yaml_document_is_empty_config():
    assert parse_sastrc("# just a comment, no content\n") == parse_sastrc("")


def test_parse_sastrc_rejects_non_mapping_exclusions():
    with pytest.raises(ValueError, match="'exclusions' must be a mapping"):
        parse_sastrc("exclusions: [\"vendor/**\"]\n")


def test_parse_sastrc_rejects_non_list_ignore():
    with pytest.raises(ValueError, match="'ignore' must be a list"):
        parse_sastrc('ignore:\n  rule: "*"\n')


def test_parse_sastrc_rejects_non_mapping_ignore_entry():
    with pytest.raises(ValueError, match="ignore\\[0\\] must be a mapping"):
        parse_sastrc("ignore:\n  - just-a-string\n")


def test_parse_sastrc_rejects_non_mapping_top_level():
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        parse_sastrc("- just\n- a\n- list\n")


def test_parse_sastrc_rejects_malformed_yaml():
    with pytest.raises(ValueError, match="not valid YAML"):
        parse_sastrc("exclusions: [unterminated\n")


# --- translate_gitignore_pattern / parse_sastignore --------------------


def test_translate_bare_name_prefixes_any_depth_glob():
    result = translate_gitignore_pattern("*.log")
    assert result == ["**/*.log", "**/*.log/**"]


def test_translate_directory_only_pattern():
    assert translate_gitignore_pattern("node_modules/") == ["**/node_modules/**"]


def test_translate_leading_slash_anchors_to_root():
    assert translate_gitignore_pattern("/build") == ["build", "build/**"]


def test_translate_mid_path_anchors_to_root():
    assert translate_gitignore_pattern("src/generated/") == ["src/generated/**"]


def test_translate_double_star_passes_through():
    result = translate_gitignore_pattern("**/fixtures/**")
    assert result[0] == "**/fixtures/**"


def test_translate_negation_raises():
    with pytest.raises(ValueError, match="negated pattern"):
        translate_gitignore_pattern("!keep-this.log")


def test_parse_sastignore_skips_comments_and_blank_lines():
    text = "# a comment\n\n*.log\n"
    assert parse_sastignore(text) == ["**/*.log", "**/*.log/**"]


def test_parse_sastignore_empty_text():
    assert parse_sastignore("") == []


def test_parse_sastignore_propagates_negation_error():
    with pytest.raises(ValueError, match="negated pattern"):
        parse_sastignore("*.log\n!important.log\n")


# --- build_scan_properties (the merge) ----------------------------------


def test_build_scan_properties_neither_file_present_is_a_noop():
    assert build_scan_properties(None, None) == []
    assert build_scan_properties("", "") == []


def test_build_scan_properties_only_sastrc_exclusions():
    props = build_scan_properties("exclusions:\n  paths: [\"vendor/**\"]\n", None)
    assert props == ["sonar.exclusions=vendor/**"]


def test_build_scan_properties_only_sastignore():
    props = build_scan_properties(None, "*.log\n")
    assert props == ["sonar.exclusions=**/*.log,**/*.log/**"]


def test_build_scan_properties_merges_sastrc_and_sastignore_exclusions():
    props = build_scan_properties("exclusions:\n  paths: [\"vendor/**\"]\n", "*.log\n")
    assert props == ["sonar.exclusions=vendor/**,**/*.log,**/*.log/**"]


def test_build_scan_properties_coverage_and_duplication_exclusions():
    text = """
exclusions:
  coverage_paths: ["**/mocks/**"]
  duplication_paths: ["**/testdata/**"]
"""
    props = build_scan_properties(text, None)
    assert "sonar.coverage.exclusions=**/mocks/**" in props
    assert "sonar.cpd.exclusions=**/testdata/**" in props


def test_build_scan_properties_tests_paths_keeps_sources_at_shared_root():
    # sonar.tests only accepts directories and rejects globs fatally, and
    # narrowing it below sonar.sources makes SonarQube silently scan only
    # that directory - so both must stay at the project root, with the
    # caller's patterns carried by sonar.test.inclusions instead.
    text = """
tests:
  paths: ["src/**/*.test.ts", "features/**"]
"""
    props = build_scan_properties(text, None)
    assert props == [
        "sonar.sources=.",
        "sonar.tests=.",
        "sonar.test.inclusions=src/**/*.test.ts,features/**",
    ]


def test_build_scan_properties_tests_paths_expands_bare_directory():
    # A bare directory (no glob character) must still select everything
    # under it once it's carried by sonar.test.inclusions rather than
    # sonar.tests, or nothing in that directory would be treated as a test.
    props = build_scan_properties('tests:\n  paths: ["features"]\n', None)
    assert props == [
        "sonar.sources=.",
        "sonar.tests=.",
        "sonar.test.inclusions=features/**",
    ]


def test_build_scan_properties_ignore_criteria_multicriteria_ids():
    text = """
ignore:
  - rule: "python:S101"
    paths: ["tests/**", "scratch/**"]
"""
    props = build_scan_properties(text, None)
    assert "sonar.issue.ignore.multicriteria=e1,e2" in props
    assert "sonar.issue.ignore.multicriteria.e1.ruleKey=python:S101" in props
    assert "sonar.issue.ignore.multicriteria.e1.resourceKey=tests/**" in props
    assert "sonar.issue.ignore.multicriteria.e2.ruleKey=python:S101" in props
    assert "sonar.issue.ignore.multicriteria.e2.resourceKey=scratch/**" in props


def test_build_scan_properties_scm_disabled_defaults_off():
    assert build_scan_properties(None, None) == []


def test_build_scan_properties_scm_disabled_true_adds_property():
    props = build_scan_properties(None, None, scm_disabled=True)
    assert props == ["sonar.scm.disabled=true"]


def test_build_scan_properties_scm_disabled_combines_with_sastrc():
    props = build_scan_properties(
        "exclusions:\n  paths: [\"vendor/**\"]\n", None, scm_disabled=True
    )
    assert props == ["sonar.scm.disabled=true", "sonar.exclusions=vendor/**"]


# --- parse_sastrc: error messages ------------------------------------------


def test_parse_sastrc_yaml_error_includes_offending_content():
    # .sastrc has already passed read_config_file()'s checks by the time it
    # reaches parse_sastrc (regular file, inside project-base-dir, no
    # symlink) - at that point it's just the repo's own file, and PyYAML's
    # own message (which quotes the offending line) is useful enough to be
    # worth including, as it was before path validation was added.
    marker = "UNIQUE-CONTENT-MARKER-7f3a"
    text = f"exclusions:\n  paths: [{marker}\n  other: {marker}: :\n"
    with pytest.raises(ValueError) as excinfo:
        parse_sastrc(text)
    message = str(excinfo.value)
    assert "not valid YAML" in message
    assert marker in message


def test_parse_sastrc_yaml_error_includes_underlying_message_without_position(monkeypatch):
    import build_scan_config

    def _raise(_text):
        raise build_scan_config.yaml.YAMLError("UNIQUE-CONTENT-MARKER-7f3a")

    monkeypatch.setattr(build_scan_config.yaml, "safe_load", _raise)
    with pytest.raises(ValueError) as excinfo:
        parse_sastrc("exclusions: {}\n")
    message = str(excinfo.value)
    assert "not valid YAML" in message
    assert "UNIQUE-CONTENT-MARKER-7f3a" in message


# --- read_config_file: path validation ---------------------------------------


@pytest.fixture
def project(tmp_path):
    base = tmp_path / "project"
    base.mkdir()
    return base


def test_read_config_file_reads_regular_file_inside_project(project):
    from build_scan_config import read_config_file

    (project / ".sastrc").write_text("exclusions:\n  paths: [\"vendor/**\"]\n")
    assert read_config_file(str(project / ".sastrc"), str(project)) == (
        "exclusions:\n  paths: [\"vendor/**\"]\n"
    )


def test_read_config_file_reads_regular_file_in_subdirectory(project):
    from build_scan_config import read_config_file

    (project / "config").mkdir()
    (project / "config" / "sastignore").write_text("*.log\n")
    assert read_config_file(str(project / "config" / "sastignore"), str(project)) == "*.log\n"


def test_read_config_file_missing_file_is_none(project):
    from build_scan_config import read_config_file

    assert read_config_file(str(project / ".sastrc"), str(project)) is None


def test_read_config_file_missing_parent_directory_is_none(project):
    from build_scan_config import read_config_file

    assert read_config_file(str(project / "nope" / ".sastrc"), str(project)) is None


def test_read_config_file_accepts_relative_paths(project, monkeypatch):
    from build_scan_config import read_config_file

    (project / ".sastignore").write_text("*.log\n")
    monkeypatch.chdir(project.parent)
    assert read_config_file("project/.sastignore", "project") == "*.log\n"


def test_read_config_file_refuses_symlinked_file(project, tmp_path):
    from build_scan_config import read_config_file

    outside = tmp_path / "outside.txt"
    outside.write_text("not project content\n")
    (project / ".sastignore").symlink_to(outside)
    with pytest.raises(ValueError, match="symbolic link"):
        read_config_file(str(project / ".sastignore"), str(project))


def test_read_config_file_refuses_symlink_even_when_target_is_inside_project(project):
    from build_scan_config import read_config_file

    (project / "real-sastrc").write_text("exclusions: {}\n")
    (project / ".sastrc").symlink_to(project / "real-sastrc")
    with pytest.raises(ValueError, match="symbolic link"):
        read_config_file(str(project / ".sastrc"), str(project))


def test_read_config_file_refuses_dangling_symlink(project, tmp_path):
    from build_scan_config import read_config_file

    (project / ".sastrc").symlink_to(tmp_path / "does-not-exist")
    with pytest.raises(ValueError, match="symbolic link"):
        read_config_file(str(project / ".sastrc"), str(project))


def test_read_config_file_refuses_symlinked_parent_directory(project, tmp_path):
    from build_scan_config import read_config_file

    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "sastignore").write_text("not project content\n")
    (project / "config").symlink_to(outside_dir, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic link"):
        read_config_file(str(project / "config" / "sastignore"), str(project))


def test_read_config_file_refuses_path_outside_project(project, tmp_path):
    from build_scan_config import read_config_file

    outside = tmp_path / "outside.txt"
    outside.write_text("not project content\n")
    with pytest.raises(ValueError, match="outside the project directory"):
        read_config_file(str(project / ".." / "outside.txt"), str(project))


def test_read_config_file_refuses_absolute_path_outside_project(project, tmp_path):
    from build_scan_config import read_config_file

    outside = tmp_path / "outside.txt"
    outside.write_text("not project content\n")
    with pytest.raises(ValueError, match="outside the project directory"):
        read_config_file(str(outside), str(project))


def test_read_config_file_refuses_project_base_dir_itself(project):
    from build_scan_config import read_config_file

    with pytest.raises(ValueError, match="not a regular file"):
        read_config_file(str(project), str(project))


def test_read_config_file_refuses_directory(project):
    from build_scan_config import read_config_file

    (project / ".sastrc").mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        read_config_file(str(project / ".sastrc"), str(project))


def test_read_config_file_refuses_fifo(project):
    from build_scan_config import read_config_file

    os.mkfifo(project / ".sastignore")
    with pytest.raises(ValueError, match="not a regular file"):
        read_config_file(str(project / ".sastignore"), str(project))


def test_read_config_file_works_when_project_base_dir_is_reached_via_symlink(tmp_path):
    # The project directory itself may sit behind a symlink (e.g. a
    # runner's workspace path); only components below it are checked.
    from build_scan_config import read_config_file

    real = tmp_path / "real-project"
    real.mkdir()
    (real / ".sastrc").write_text("exclusions: {}\n")
    link = tmp_path / "linked-project"
    link.symlink_to(real, target_is_directory=True)
    assert read_config_file(str(link / ".sastrc"), str(link)) == "exclusions: {}\n"


def test_read_config_file_error_does_not_include_file_content(project, tmp_path):
    from build_scan_config import read_config_file

    outside = tmp_path / "outside.txt"
    outside.write_text("UNIQUE-CONTENT-MARKER-7f3a\n")
    (project / ".sastignore").symlink_to(outside)
    with pytest.raises(ValueError) as excinfo:
        read_config_file(str(project / ".sastignore"), str(project))
    assert "UNIQUE-CONTENT-MARKER-7f3a" not in str(excinfo.value)


# --- CLI ---------------------------------------------------------------------


def _run_cli(tmp_path, project):
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parent.parent / "scripts" / "build_scan_config.py"
    out = tmp_path / "args.bin"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--config",
            str(project / ".sastrc"),
            "--ignore-file",
            str(project / ".sastignore"),
            "--project-base-dir",
            str(project),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
    )
    return proc, out


def test_cli_writes_properties_for_regular_files(project, tmp_path):
    (project / ".sastrc").write_text("exclusions:\n  paths: [\"vendor/**\"]\n")
    (project / ".sastignore").write_text("*.log\n")
    proc, out = _run_cli(tmp_path, project)
    assert proc.returncode == 0, proc.stderr
    assert out.read_bytes() == b"-Dsonar.exclusions=vendor/**,**/*.log,**/*.log/**\0"


def test_cli_with_no_config_files_writes_nothing(project, tmp_path):
    proc, out = _run_cli(tmp_path, project)
    assert proc.returncode == 0, proc.stderr
    assert out.read_bytes() == b""


def test_cli_refuses_symlinked_ignore_file_without_echoing_content(project, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("UNIQUE-CONTENT-MARKER-7f3a\n")
    (project / ".sastignore").symlink_to(outside)
    proc, out = _run_cli(tmp_path, project)
    assert proc.returncode != 0
    assert "symbolic link" in proc.stderr
    assert "UNIQUE-CONTENT-MARKER-7f3a" not in proc.stdout + proc.stderr
    assert not out.exists() or b"UNIQUE-CONTENT-MARKER-7f3a" not in out.read_bytes()
