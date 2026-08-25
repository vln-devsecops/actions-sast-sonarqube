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


def test_build_scan_properties_tests_paths():
    text = """
tests:
  paths: ["src/**/*.test.ts", "features/**"]
"""
    props = build_scan_properties(text, None)
    assert props == ["sonar.tests=src/**/*.test.ts,features/**"]


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
