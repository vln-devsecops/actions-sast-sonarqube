import pytest

from detect_unanalyzed_languages import (
    detect_extension_languages,
    detect_unanalyzed_languages,
    parse_scanner_log,
)

CSHARP_WARN_LINE = (
    "WARN  Your project contains C# files which cannot be analyzed with the "
    "scanner you are using. To analyze C# or VB.NET, you must use the "
    "SonarScanner for .NET 5.x or higher, see "
    "https://redirect.sonarsource.com/doc/install-configure-scanner-msbuild.html"
)

VBNET_WARN_LINE = CSHARP_WARN_LINE.replace("C#", "VB.NET", 1)


# --- parse_scanner_log --------------------------------------------------


def test_parse_scanner_log_detects_csharp():
    assert parse_scanner_log(CSHARP_WARN_LINE) == ["C#"]


def test_parse_scanner_log_detects_vbnet():
    assert parse_scanner_log(VBNET_WARN_LINE) == ["VB.NET"]


def test_parse_scanner_log_returns_empty_list_when_no_warning_present():
    log = "INFO  Analysis report generated in 123ms\nINFO  ANALYSIS SUCCESSFUL\n"
    assert parse_scanner_log(log) == []


def test_parse_scanner_log_ignores_unrelated_warn_lines():
    log = "WARN  Shallow clone detected, no blame information will be provided.\n"
    assert parse_scanner_log(log) == []


def test_parse_scanner_log_handles_empty_and_none_input():
    assert parse_scanner_log("") == []
    assert parse_scanner_log(None) == []


def test_parse_scanner_log_dedupes_repeated_lines_preserving_first_order():
    log = "\n".join([CSHARP_WARN_LINE, CSHARP_WARN_LINE])
    assert parse_scanner_log(log) == ["C#"]


def test_parse_scanner_log_finds_multiple_distinct_languages():
    log = "\n".join([CSHARP_WARN_LINE, VBNET_WARN_LINE])
    result = parse_scanner_log(log)
    assert set(result) == {"C#", "VB.NET"}


def test_parse_scanner_log_matches_only_within_the_full_fixed_sentence():
    """A line that merely mentions 'cannot be analyzed' in some unrelated
    context must not be mistaken for the real warning."""
    log = "WARN  Something else entirely cannot be analyzed today.\n"
    assert parse_scanner_log(log) == []


# --- detect_extension_languages ----------------------------------------


def _touch(path, content="// stub\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_detect_extension_languages_finds_c(tmp_path):
    _touch(tmp_path / "src" / "main.c")
    assert detect_extension_languages(tmp_path) == ["C"]


@pytest.mark.parametrize("ext", [".cc", ".cpp", ".cxx", ".c++"])
def test_detect_extension_languages_finds_cpp_variants(tmp_path, ext):
    _touch(tmp_path / f"main{ext}")
    assert detect_extension_languages(tmp_path) == ["C++"]


@pytest.mark.parametrize("ext", [".m", ".mm"])
def test_detect_extension_languages_finds_objective_c_variants(tmp_path, ext):
    _touch(tmp_path / f"AppDelegate{ext}")
    assert detect_extension_languages(tmp_path) == ["Objective-C"]


def test_detect_extension_languages_finds_swift(tmp_path):
    _touch(tmp_path / "App.swift")
    assert detect_extension_languages(tmp_path) == ["Swift"]


def test_detect_extension_languages_ignores_bare_h(tmp_path):
    """`.h` is ambiguous (C, C++, or Objective-C header) - deliberately not
    counted on its own."""
    _touch(tmp_path / "widget.h")
    assert detect_extension_languages(tmp_path) == []


def test_detect_extension_languages_ignores_unrelated_extensions(tmp_path):
    _touch(tmp_path / "app.py")
    _touch(tmp_path / "app.js")
    assert detect_extension_languages(tmp_path) == []


def test_detect_extension_languages_returns_empty_for_empty_tree(tmp_path):
    assert detect_extension_languages(tmp_path) == []


def test_detect_extension_languages_ignores_git_directory(tmp_path):
    _touch(tmp_path / ".git" / "weird.c")
    assert detect_extension_languages(tmp_path) == []


def test_detect_extension_languages_ignores_scannerwork_directory(tmp_path):
    _touch(tmp_path / ".scannerwork" / "cache.cpp")
    assert detect_extension_languages(tmp_path) == []


def test_detect_extension_languages_ignores_sonarqube_directory(tmp_path):
    _touch(tmp_path / ".sonarqube" / "out" / "leftover.swift")
    assert detect_extension_languages(tmp_path) == []


def test_detect_extension_languages_sorts_and_dedupes_results(tmp_path):
    _touch(tmp_path / "a.swift")
    _touch(tmp_path / "b.swift")
    _touch(tmp_path / "c.c")
    assert detect_extension_languages(tmp_path) == ["C", "Swift"]


def test_detect_extension_languages_honors_exclusion_globs(tmp_path):
    _touch(tmp_path / "vendor" / "lib.cpp")
    assert detect_extension_languages(tmp_path, exclude_patterns=["**/vendor/**"]) == []


def test_detect_extension_languages_without_exclusions_still_finds_vendored_file(tmp_path):
    _touch(tmp_path / "vendor" / "lib.cpp")
    assert detect_extension_languages(tmp_path, exclude_patterns=[]) == ["C++"]


def test_detect_extension_languages_exclusion_does_not_overreach(tmp_path):
    """An exclusion pattern for one path must not blind the detector to an
    unrelated file elsewhere in the tree."""
    _touch(tmp_path / "vendor" / "lib.cpp")
    _touch(tmp_path / "src" / "main.c")
    result = detect_extension_languages(tmp_path, exclude_patterns=["**/vendor/**"])
    assert result == ["C"]


def test_detect_extension_languages_single_star_does_not_cross_directories(tmp_path):
    """A lone '*' (unlike '**') must not match a '/' - 'vendor/*.cpp'
    excludes a direct child but not a file nested one level deeper."""
    _touch(tmp_path / "vendor" / "lib.cpp")
    _touch(tmp_path / "vendor" / "sub" / "deep.cpp")
    result = detect_extension_languages(tmp_path, exclude_patterns=["vendor/*.cpp"])
    assert result == ["C++"]  # the nested one is still reported


def test_detect_extension_languages_question_mark_matches_one_character(tmp_path):
    _touch(tmp_path / "lib1.cpp")
    result = detect_extension_languages(tmp_path, exclude_patterns=["lib?.cpp"])
    assert result == []


def test_detect_extension_languages_question_mark_does_not_match_zero_or_two_characters(tmp_path):
    _touch(tmp_path / "lib.cpp")
    _touch(tmp_path / "lib12.cpp")
    result = detect_extension_languages(tmp_path, exclude_patterns=["lib?.cpp"])
    assert result == ["C++"]


def test_detect_extension_languages_anchored_exclusion_pattern(tmp_path):
    """A bare top-level directory pattern (as .sastignore's translator
    produces for e.g. 'excluded/') excludes only that path, not a
    similarly-named file deeper in the tree."""
    _touch(tmp_path / "excluded" / "skip.swift")
    _touch(tmp_path / "nested" / "excluded" / "keep.swift")
    result = detect_extension_languages(tmp_path, exclude_patterns=["excluded/**"])
    assert result == ["Swift"]


# --- detect_unanalyzed_languages (combined) -----------------------------


def test_detect_unanalyzed_languages_combines_log_and_extension_results(tmp_path):
    _touch(tmp_path / "native.c")
    result = detect_unanalyzed_languages(CSHARP_WARN_LINE, tmp_path)
    assert result == ["C", "C#"]


def test_detect_unanalyzed_languages_empty_when_nothing_detected(tmp_path):
    _touch(tmp_path / "app.py")
    assert detect_unanalyzed_languages("INFO  ANALYSIS SUCCESSFUL\n", tmp_path) == []


def test_detect_unanalyzed_languages_dedupes_overlap(tmp_path):
    """Extension detection and log detection are expected to be disjoint in
    practice (no Community Build plugin logs the C/C++/Swift warning), but
    the combination must still not emit a duplicate if they ever overlap."""
    _touch(tmp_path / "native.c")
    log = "\n".join([CSHARP_WARN_LINE, CSHARP_WARN_LINE])
    result = detect_unanalyzed_languages(log, tmp_path)
    assert result == ["C", "C#"]


def test_detect_unanalyzed_languages_respects_exclusion_patterns(tmp_path):
    _touch(tmp_path / "vendor" / "lib.cpp")
    result = detect_unanalyzed_languages("", tmp_path, exclude_patterns=["**/vendor/**"])
    assert result == []
