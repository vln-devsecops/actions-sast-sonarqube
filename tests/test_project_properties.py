import pytest

from project_properties import (
    ALLOWED_PROJECT_PROPERTIES,
    filter_project_properties,
    format_dropped_keys,
    parse_properties,
    sanitize_key_name,
)

# --- parse_properties: separators ---------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "sonar.sources=src",
        "sonar.sources:src",
        "sonar.sources src",
        "sonar.sources\tsrc",
        "sonar.sources\fsrc",
        "sonar.sources = src",
        "sonar.sources : src",
        "sonar.sources   src",
        "   sonar.sources=src",
        "\tsonar.sources=src",
    ],
)
def test_parse_properties_accepts_every_separator_form(line):
    assert parse_properties(line + "\n") == {"sonar.sources": "src"}


def test_parse_properties_only_first_separator_splits():
    assert parse_properties("a=b=c\n") == {"a": "b=c"}
    assert parse_properties("a:b:c\n") == {"a": "b:c"}
    assert parse_properties("a = = b\n") == {"a": "= b"}


def test_parse_properties_whitespace_then_separator_is_one_separator():
    assert parse_properties("a  =  b\n") == {"a": "b"}
    assert parse_properties("a\t:\tb\n") == {"a": "b"}


def test_parse_properties_key_without_value_is_empty_string():
    assert parse_properties("a\nb=\nc :\n") == {"a": "", "b": "", "c": ""}


def test_parse_properties_last_duplicate_wins():
    assert parse_properties("a=1\na=2\n") == {"a": "2"}


def test_parse_properties_values_are_trimmed():
    # The scanner trims every value it loads (Conf.toProperties).
    assert parse_properties("a=  b  \n") == {"a": "b"}
    assert parse_properties("a=b\\t\n") == {"a": "b"}
    assert parse_properties("a=\\t\\nb\n") == {"a": "b"}


# --- parse_properties: comments and blank lines --------------------------


def test_parse_properties_skips_comments_and_blank_lines():
    text = "# comment\n! also a comment\n\n   \n  # indented comment\na=1\n"
    assert parse_properties(text) == {"a": "1"}


def test_parse_properties_hash_after_key_is_part_of_value():
    assert parse_properties("a=b # not a comment\n") == {"a": "b # not a comment"}


def test_parse_properties_comment_line_is_never_continued():
    assert parse_properties("# comment \\\na=1\n") == {"a": "1"}


# --- parse_properties: line endings and continuation ---------------------


@pytest.mark.parametrize("eol", ["\n", "\r\n", "\r"])
def test_parse_properties_handles_every_line_ending(eol):
    assert parse_properties(f"a=1{eol}b=2{eol}") == {"a": "1", "b": "2"}


def test_parse_properties_continuation_joins_lines_and_drops_leading_whitespace():
    text = "sonar.exclusions=a/**,\\\n    b/**,\\\n\tc/**\n"
    assert parse_properties(text) == {"sonar.exclusions": "a/**,b/**,c/**"}


def test_parse_properties_continuation_line_starting_with_hash_is_not_a_comment():
    assert parse_properties("a=1\\\n#2\n") == {"a": "1#2"}


def test_parse_properties_even_backslashes_do_not_continue():
    assert parse_properties("a=x\\\\\nb=y\n") == {"a": "x\\", "b": "y"}


def test_parse_properties_odd_backslashes_continue():
    assert parse_properties("a=x\\\\\\\ny\n") == {"a": "x\\y"}


def test_parse_properties_empty_continuation_line_ends_the_entry():
    assert parse_properties("a=x\\\n\nb=y\n") == {"a": "x", "b": "y"}


def test_parse_properties_line_empty_after_continuation_starts_over():
    # Matches Properties.load: the next line may then be a comment or blank.
    assert parse_properties("\\\n#a=1\nb=2\n") == {"b": "2"}
    assert parse_properties("  \\\n\n\\\n!c\nb=2\n") == {"b": "2"}
    assert parse_properties("\\\n  a=1\n") == {"a": "1"}


def test_parse_properties_continuation_at_end_of_file_is_dropped():
    assert parse_properties("a=x\\") == {"a": "x"}


def test_parse_properties_continuation_can_split_a_key():
    assert parse_properties("sonar.\\\n  sources=src\n") == {"sonar.sources": "src"}


# --- parse_properties: escapes --------------------------------------------


def test_parse_properties_standard_escapes():
    assert parse_properties("a=x\\ty\\nz\\rw\\fv\n") == {"a": "x\ty\nz\rw\fv"}


def test_parse_properties_unicode_escape_in_value_and_key():
    assert parse_properties("sonar.sour\\u0063es=\\u0073rc\n") == {"sonar.sources": "src"}


def test_parse_properties_unicode_escape_is_case_insensitive_hex():
    assert parse_properties("a=\\u00E9\\u00e9\n") == {"a": "\u00e9\u00e9"}


def test_parse_properties_unicode_surrogate_pair_is_combined():
    assert parse_properties("a=\\uD83D\\uDE00\n") == {"a": "\U0001F600"}


def test_parse_properties_escaped_separators_and_spaces_stay_in_key():
    assert parse_properties("a\\=b\\:c\\ d=v\n") == {"a=b:c d": "v"}


def test_parse_properties_unknown_escape_drops_backslash():
    assert parse_properties("a=\\q\\\\\n") == {"a": "q\\"}


@pytest.mark.parametrize("bad", ["a=\\u12\n", "a=\\uZZZZ\n", "a=\\u\n", "\\u00g1=x\n"])
def test_parse_properties_malformed_unicode_escape_fails_closed(bad):
    with pytest.raises(ValueError, match="malformed"):
        parse_properties(bad)


def test_parse_properties_keeps_non_ascii_text():
    assert parse_properties("a=caf\u00e9\n") == {"a": "caf\u00e9"}


def test_parse_properties_empty_text():
    assert parse_properties("") == {}


# --- filter_project_properties -------------------------------------------


def test_allowlist_is_small_and_explicit():
    assert ALLOWED_PROJECT_PROPERTIES == {
        "sonar.sources",
        "sonar.tests",
        "sonar.inclusions",
        "sonar.exclusions",
        "sonar.test.inclusions",
        "sonar.test.exclusions",
        "sonar.coverage.exclusions",
        "sonar.cpd.exclusions",
        "sonar.sourceEncoding",
        "sonar.python.version",
        "sonar.java.source",
    }


def test_filter_keeps_allowlisted_keys_in_file_order():
    props = {"sonar.exclusions": "vendor/**", "sonar.sources": "src", "sonar.sourceEncoding": "UTF-8"}
    kept, dropped = filter_project_properties(props)
    assert kept == props
    assert list(kept) == ["sonar.exclusions", "sonar.sources", "sonar.sourceEncoding"]
    assert dropped == []


@pytest.mark.parametrize(
    "key",
    [
        "sonar.host.url",
        "sonar.token",
        "sonar.login",
        "sonar.password",
        "sonar.userHome",
        "sonar.working.directory",
        "sonar.projectKey",
        "sonar.projectBaseDir",
        "project.settings",
        "scanner.settings",
        "sonar.modules",
        "module1.sonar.sources",
        "sonar.scanner.javaExePath",
        "sonar.scanner.javaOpts",
        "sonar.scanner.engineJarPath",
        "sonar.scanner.skipJreProvisioning",
        "sonar.scanner.proxyHost",
        "sonar.scanner.proxyPort",
        "sonar.scanner.truststorePath",
        "sonar.scanner.metadataFilePath",
        "sonar.nodejs.executable",
        "sonar.scm.disabled",
        "sonar.issue.ignore.multicriteria",
        "sonar.projectVersion",
        "SONAR.SOURCES",
        "sonar.sources.extra",
    ],
)
def test_filter_drops_everything_not_allowlisted(key):
    kept, dropped = filter_project_properties({key: "x", "sonar.sources": "src"})
    assert kept == {"sonar.sources": "src"}
    assert dropped == [key]


@pytest.mark.parametrize(
    "key",
    [
        "sonar.sources",
        "sonar.tests",
        "sonar.inclusions",
        "sonar.exclusions",
        "sonar.test.inclusions",
        "sonar.test.exclusions",
        "sonar.coverage.exclusions",
        "sonar.cpd.exclusions",
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        "/etc",
        "src,/etc/**",
        "\\\\server\\share",
        "C:/project",
        "c:\\project",
        "..",
        "../sibling/**",
        "src/../../x",
        "src\\..\\..\\x",
        "src/..",
    ],
)
def test_filter_rejects_absolute_and_parent_paths(key, value):
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        filter_project_properties({key: value})


@pytest.mark.parametrize(
    "value",
    ["src", "src,test", "src, test", "**/vendor/**", "./src", "a..b/**", "src/..x", "", "a/*.min.js,b/**/c"],
)
def test_filter_accepts_relative_paths_and_globs(value):
    kept, _ = filter_project_properties({"sonar.exclusions": value})
    assert kept == {"sonar.exclusions": value}


def test_filter_error_does_not_echo_the_value():
    with pytest.raises(ValueError) as excinfo:
        filter_project_properties({"sonar.sources": "/UNIQUE-VALUE-MARKER-9c1d"})
    assert "UNIQUE-VALUE-MARKER-9c1d" not in str(excinfo.value)


@pytest.mark.parametrize(
    "key,value",
    [
        ("sonar.sourceEncoding", "UTF-8"),
        ("sonar.sourceEncoding", "ISO-8859-1"),
        ("sonar.sourceEncoding", "windows-1252"),
        ("sonar.python.version", "3"),
        ("sonar.python.version", "3.12"),
        ("sonar.python.version", "3.9, 3.10,3.11"),
        ("sonar.java.source", "17"),
        ("sonar.java.source", "1.8"),
    ],
)
def test_filter_accepts_well_formed_scalar_values(key, value):
    kept, _ = filter_project_properties({key: value})
    assert kept == {key: value}


@pytest.mark.parametrize(
    "key,value",
    [
        ("sonar.sourceEncoding", "UTF 8"),
        ("sonar.sourceEncoding", ""),
        ("sonar.sourceEncoding", "UTF-8,x"),
        ("sonar.python.version", "three"),
        ("sonar.python.version", "3.12;x"),
        ("sonar.python.version", ""),
        ("sonar.java.source", "17 -x"),
        ("sonar.java.source", "v17"),
    ],
)
def test_filter_rejects_malformed_scalar_values(key, value):
    with pytest.raises(ValueError, match=key.replace(".", r"\.")):
        filter_project_properties({key: value})


@pytest.mark.parametrize("value", ["src/${env.HOME}", "${sonar.host.url}", "a\nb", "a\x00b", "a\x7fb"])
def test_filter_rejects_placeholders_and_control_characters(value):
    with pytest.raises(ValueError, match=r"sonar\.exclusions"):
        filter_project_properties({"sonar.exclusions": value})


def test_filter_reports_dropped_keys_in_file_order():
    _, dropped = filter_project_properties({"b.x": "1", "sonar.sources": "s", "a.x": "2"})
    assert dropped == ["b.x", "a.x"]


# --- sanitize_key_name / format_dropped_keys ------------------------------


def test_sanitize_key_name_keeps_safe_characters():
    assert sanitize_key_name("sonar.scanner.java_Exe-Path9") == "sonar.scanner.java_Exe-Path9"


@pytest.mark.parametrize(
    "raw",
    ["a\nb", "a\rb", "a%0Ab", "a::b", "a b", "a\u00e9b", "a\x1bb", "a$b", "a/b"],
)
def test_sanitize_key_name_replaces_unsafe_characters(raw):
    cleaned = sanitize_key_name(raw)
    assert all(c.isascii() and (c.isalnum() or c in "._-?") for c in cleaned)
    assert "\n" not in cleaned and "%" not in cleaned and ":" not in cleaned


def test_sanitize_key_name_caps_length():
    cleaned = sanitize_key_name("a" * 500)
    assert len(cleaned) <= 80
    assert cleaned.endswith("...")


def test_sanitize_key_name_empty():
    assert sanitize_key_name("") == "?"


def test_format_dropped_keys_lists_sanitised_unique_names():
    assert format_dropped_keys(["sonar.host.url", "x\ny", "sonar.host.url"]) == "sonar.host.url, x?y"


def test_format_dropped_keys_caps_number_of_names():
    message = format_dropped_keys([f"k{i}" for i in range(50)])
    assert message.startswith("k0, k1, ")
    assert "k19" in message and "k20" not in message
    assert message.endswith("(and 30 more)")
