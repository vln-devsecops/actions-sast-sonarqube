import os
import subprocess

import pytest

from detect_unanalyzed_languages import (
    detect_extension_languages,
    detect_uncompiled_dotnet_languages,
    detect_unanalyzed_languages,
    load_exclude_patterns,
    parse_scanner_log,
)


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path):
    _git("init", "-q", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _rev_parse_head(path):
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, check=True, capture_output=True, text=True,
    ).stdout.strip()


def _register_gitlink(outer_path, submodule_relpath, sha):
    """Register a 160000 (gitlink) index entry without touching the
    working tree - `git update-index --cacheinfo` only writes the index,
    so this works whether or not anything actually exists on disk at
    submodule_relpath, exactly like a real unpopulated submodule."""
    _git("update-index", "--add", "--cacheinfo", f"160000,{sha},{submodule_relpath}", cwd=outer_path)


def _make_populated_submodule(outer_path, submodule_relpath, filename="x.cpp"):
    """Create a real nested git repo on disk at submodule_relpath and
    register it as a gitlink in outer_path's index - mirrors what a real,
    initialized `git submodule` looks like, without needing network
    access (no `git submodule add`, which needs a transport)."""
    submodule_abs = outer_path / submodule_relpath
    submodule_abs.mkdir(parents=True)
    _init_repo(submodule_abs)
    _touch(submodule_abs / filename)
    _git("add", "-A", cwd=submodule_abs)
    _git("commit", "-q", "-m", "init", cwd=submodule_abs)
    _register_gitlink(outer_path, submodule_relpath, _rev_parse_head(submodule_abs))

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


def test_parse_scanner_log_matches_a_real_timestamped_line():
    """Real scanner output is prefixed with a timestamp, e.g.
    '21:36:22.088 WARN  Your project contains ...' - confirm the anchored
    pattern still matches that shape, not just a bare 'WARN ...' line."""
    log = (
        "21:36:22.088 WARN  Your project contains C# files which cannot be "
        "analyzed with the scanner you are using. To analyze C# or VB.NET, "
        "you must use the SonarScanner for .NET 5.x or higher, see "
        "https://redirect.sonarsource.com/doc/install-configure-scanner-msbuild.html"
    )
    assert parse_scanner_log(log) == ["C#"]


def test_parse_scanner_log_rejects_markdown_link_injection():
    """A captured 'language' name flows into the ::warning:: annotation,
    the findings report, the Check Run summary and the PR comment - all
    Markdown-rendered surfaces. A crafted log line must not let an
    attacker smuggle a Markdown link through as a 'language'."""
    log = "WARN  Your project contains [x](https://e.vil) files which cannot be analyzed with the scanner you are using.\n"
    assert parse_scanner_log(log) == []


def test_parse_scanner_log_rejects_mention_injection():
    log = "WARN  Your project contains @org/team files which cannot be analyzed with the scanner you are using.\n"
    assert parse_scanner_log(log) == []


def test_parse_scanner_log_rejects_a_comma_in_the_name():
    """A comma would otherwise let a crafted line smuggle in a second,
    attacker-chosen 'language' entry disguised as part of one match."""
    log = "WARN  Your project contains C#, VB.NET files which cannot be analyzed with the scanner you are using.\n"
    assert parse_scanner_log(log) == []


def test_parse_scanner_log_rejects_a_warn_sentence_not_at_the_start_of_a_line():
    """The real line is always `<timestamp> WARN  ...` - a single token
    before WARN. A line where the sentence is buried after multiple other
    words must not match, even though the substring is byte-for-byte the
    real sentence."""
    log = "noisy prefix WARN  Your project contains C# files which cannot be analyzed with the scanner you are using.\n"
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


def test_detect_extension_languages_ignores_baseline_checkout_directory(tmp_path):
    """sonar-pr.yml's fallback baseline scan checks out the target branch
    into .baseline-checkout alongside the repo root - when
    project-base-dir is '.', that directory must not be treated as the
    PR's own project source."""
    _touch(tmp_path / ".baseline-checkout" / "leftover.cpp")
    assert detect_extension_languages(tmp_path) == []


def test_detect_extension_languages_ignores_sast_sonarqube_directory(tmp_path):
    """sonar-pr.yml/sonar-baseline.yml check out this action's own source
    into .sast-sonarqube alongside the repo root."""
    _touch(tmp_path / ".sast-sonarqube" / "scripts" / "run_scan.sh.cpp")
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


# --- detect_extension_languages: inside a real git work tree -----------
#
# project-base-dir in the real sonar-pr.yml/sonar-baseline.yml workflows is
# always a checked-out git work tree, so these exercise the git-ls-files
# fast path (_candidate_relpaths) directly, not just the plain-walk
# fallback the tmp_path-only tests above exercise (pytest's tmp_path is
# never itself inside a git repo).


def test_detect_extension_languages_in_git_repo_honors_gitignore(tmp_path):
    """node-gyp-style native addons under a gitignored node_modules/ (or a
    gitignored .venv/, vendored SDK, etc.) must not be reported - the
    consumer's own .gitignore already says this isn't their project
    source."""
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    _touch(tmp_path / "node_modules" / "native-addon" / "binding.cc")
    _touch(tmp_path / "app" / "View.swift")
    assert detect_extension_languages(tmp_path) == ["Swift"]


def test_detect_extension_languages_in_git_repo_still_finds_ungitignored_files(tmp_path):
    """Confirms the git fast path doesn't just report nothing - an
    untracked, not-ignored file is still found."""
    _init_repo(tmp_path)
    _touch(tmp_path / "native.cpp")
    assert detect_extension_languages(tmp_path) == ["C++"]


def test_detect_extension_languages_in_git_repo_prunes_baseline_checkout_even_if_not_gitignored(tmp_path):
    """Defense in depth: .baseline-checkout must be pruned even when the
    consumer's own .gitignore says nothing about it (it's this action's
    own workflow scratch directory, not the consumer's concern)."""
    _init_repo(tmp_path)
    _touch(tmp_path / ".baseline-checkout" / "leftover.cpp")
    _touch(tmp_path / "native.c")
    assert detect_extension_languages(tmp_path) == ["C"]


def test_detect_extension_languages_in_git_repo_prunes_sast_sonarqube_even_if_not_gitignored(tmp_path):
    _init_repo(tmp_path)
    _touch(tmp_path / ".sast-sonarqube" / "fixtures" / "leftover.swift")
    _touch(tmp_path / "native.c")
    assert detect_extension_languages(tmp_path) == ["C"]


def test_detect_extension_languages_does_not_descend_into_a_nested_git_repo(tmp_path):
    """An untracked nested repo (e.g. .baseline-checkout, created by
    `actions/checkout` with its own .git) is exactly what sonar-pr.yml
    leaves behind. git itself doesn't recurse into an embedded repository
    with no submodule record, so the outer `git ls-files` run from
    project-base-dir never sees its contents - confirmed empirically."""
    _init_repo(tmp_path)
    _touch(tmp_path / "native.c")
    nested = tmp_path / "some-other-nested-repo"
    _touch(nested / "inner.cpp")
    _init_repo(nested)
    assert detect_extension_languages(tmp_path) == ["C"]


def test_detect_extension_languages_falls_back_to_walk_when_git_binary_is_missing(tmp_path, monkeypatch):
    """If git itself can't be invoked (not installed, PATH issue, ...),
    fall back to the plain walk rather than erroring out the whole
    detection step over a missing optimization."""
    import detect_unanalyzed_languages as dul

    _init_repo(tmp_path)
    _touch(tmp_path / "native.c")

    def fake_run(*args, **kwargs):
        raise OSError("git not found")

    monkeypatch.setattr(dul.subprocess, "run", fake_run)
    assert detect_extension_languages(tmp_path) == ["C"]


def test_detect_extension_languages_falls_back_to_walk_when_ls_files_fails(tmp_path, monkeypatch):
    """`rev-parse` can report "true" while `ls-files` itself still fails
    (e.g. a corrupt index) - fall back rather than crash."""
    import subprocess as subprocess_module

    import detect_unanalyzed_languages as dul

    _init_repo(tmp_path)
    _touch(tmp_path / "native.c")

    real_run = dul.subprocess.run

    def fake_run(args, **kwargs):
        if "ls-files" in args:
            raise subprocess_module.CalledProcessError(1, args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(dul.subprocess, "run", fake_run)
    assert detect_extension_languages(tmp_path) == ["C"]


def test_detect_extension_languages_in_git_repo_still_honors_sastrc_exclusions(tmp_path):
    """The git fast path must not bypass the consumer's own
    .sastrc/.sastignore exclusions - only the fixed PRUNED_DIR_NAMES and
    whatever their own .gitignore already covers."""
    _init_repo(tmp_path)
    _touch(tmp_path / "vendor" / "lib.cpp")
    _touch(tmp_path / "native.c")
    result = detect_extension_languages(tmp_path, exclude_patterns=["**/vendor/**"])
    assert result == ["C"]


# --- detect_extension_languages: non-UTF-8 filenames --------------------


def test_detect_extension_languages_in_git_repo_handles_non_utf8_filename(tmp_path):
    """`git ls-files -z` emits raw bytes - a filename containing a byte
    that isn't valid UTF-8 (reproduced here exactly as $'bad\\xff.cpp'
    would appear in a shell) must not crash detection with a
    UnicodeDecodeError. A scan that passes today must not start failing
    just because of an odd filename - run_scan.sh has `set -e`."""
    _init_repo(tmp_path)
    bad_path = os.path.join(os.fsencode(str(tmp_path)), b"bad\xffname.cpp")
    with open(bad_path, "wb") as f:
        f.write(b"// stub\n")
    assert detect_extension_languages(tmp_path) == ["C++"]


def test_detect_extension_languages_falls_back_to_walk_also_handles_non_utf8_filename(tmp_path):
    """Same guarantee outside a git work tree - os.walk already
    surrogate-escapes invalid filename bytes on POSIX, so this should
    hold without any extra handling, but confirm it explicitly."""
    bad_path = os.path.join(os.fsencode(str(tmp_path)), b"bad\xffname.cpp")
    with open(bad_path, "wb") as f:
        f.write(b"// stub\n")
    assert detect_extension_languages(tmp_path) == ["C++"]


# --- detect_extension_languages: git submodules --------------------------


def test_detect_extension_languages_finds_files_in_a_populated_submodule(tmp_path):
    """`git ls-files` lists a submodule's own gitlink entry, never its
    contents (and --recurse-submodules can't combine with the -o flag
    the main listing needs for untracked-but-not-ignored files) - a
    populated submodule must still be walked."""
    _init_repo(tmp_path)
    _touch(tmp_path / "app.py")
    _make_populated_submodule(tmp_path, "vendor/sub")
    assert detect_extension_languages(tmp_path) == ["C++"]


def test_detect_extension_languages_handles_an_unpopulated_submodule(tmp_path):
    """Before `git submodule update --init`, the gitlink is registered
    in the index but the directory on disk is empty (or absent) - must
    not crash, and must report nothing for it."""
    _init_repo(tmp_path)
    _touch(tmp_path / "native.c")
    (tmp_path / "vendor" / "unpop").mkdir(parents=True)
    _register_gitlink(tmp_path, "vendor/unpop", "1234567890abcdef1234567890abcdef12345678")
    assert detect_extension_languages(tmp_path) == ["C"]


def test_detect_extension_languages_handles_a_missing_submodule_directory(tmp_path):
    """A gitlink entry whose directory doesn't exist on disk at all
    (e.g. a shallow/partial checkout) must not crash either."""
    _init_repo(tmp_path)
    _touch(tmp_path / "native.c")
    _register_gitlink(tmp_path, "vendor/missing", "1234567890abcdef1234567890abcdef12345678")
    assert detect_extension_languages(tmp_path) == ["C"]


def test_detect_extension_languages_degrades_to_no_submodules_when_submodule_listing_fails(tmp_path, monkeypatch):
    """The main `ls-files -co` listing can succeed while the separate
    `ls-files -s` submodule probe fails (e.g. a git version quirk) -
    that should degrade to "no submodules found", not abort the whole
    (otherwise-successful) listing."""
    import subprocess as subprocess_module

    import detect_unanalyzed_languages as dul

    _init_repo(tmp_path)
    _touch(tmp_path / "native.c")
    _make_populated_submodule(tmp_path, "vendor/sub")

    real_run = dul.subprocess.run

    def fake_run(args, **kwargs):
        if "-s" in args and "ls-files" in args:
            raise subprocess_module.CalledProcessError(1, args)
        return real_run(args, **kwargs)

    monkeypatch.setattr(dul.subprocess, "run", fake_run)
    assert detect_extension_languages(tmp_path) == ["C"]


def test_detect_extension_languages_submodule_contents_honor_sastignore_exclusion(tmp_path):
    """A submodule's files are prefixed with the submodule's own relative
    path before exclusion matching, so a consumer can exclude a whole
    vendored submodule via .sastrc/.sastignore the same way as any other
    directory."""
    _init_repo(tmp_path)
    _touch(tmp_path / "native.c")
    _make_populated_submodule(tmp_path, "vendor/sub")
    result = detect_extension_languages(tmp_path, exclude_patterns=["**/vendor/**"])
    assert result == ["C"]


def test_detect_extension_languages_nested_submodule_is_also_found(tmp_path):
    """A submodule containing its own submodule: the recursive
    _candidate_relpaths call inside a populated submodule naturally
    picks up a further-nested gitlink too."""
    _init_repo(tmp_path)
    submodule_abs = tmp_path / "vendor" / "sub"
    submodule_abs.mkdir(parents=True)
    _init_repo(submodule_abs)
    _touch(submodule_abs / "outer.cpp")
    _git("add", "-A", cwd=submodule_abs)
    _git("commit", "-q", "-m", "init", cwd=submodule_abs)
    _make_populated_submodule(submodule_abs, "inner", filename="Deep.swift")
    _git("add", "-A", cwd=submodule_abs)
    _git("commit", "-q", "-m", "add inner submodule", cwd=submodule_abs)
    _register_gitlink(tmp_path, "vendor/sub", _rev_parse_head(submodule_abs))
    assert detect_extension_languages(tmp_path) == ["C++", "Swift"]


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


# Trimmed from a real `dotnet-sonarscanner end` log (12.1.0, SonarQube 26.8)
# - the log run_scan.sh passes in for scanner-mode: dotnet.
DOTNET_END_LOG = """\
15:23:13.295  WARNING: WARN: SCM provider autodetection failed. Please use "sonar.scm.provider" to define SCM of your project, or disable the SCM Sensor in the project settings.
15:23:14.721  INFO: Sensor C# Project Type Information [csharp]
15:23:14.724  INFO: Sensor C# Project Type Information [csharp] (done) | time=2ms
15:23:16.100  INFO: ANALYSIS SUCCESSFUL, you can find the results at: http://localhost:9000/dashboard?id=k
"""


def test_detect_unanalyzed_languages_dotnet_mode_does_not_report_csharp_or_vbnet(tmp_path):
    _touch(tmp_path / "Tiny.Core" / "Greeter.cs")
    _touch(tmp_path / "Legacy" / "Module1.vb")
    assert detect_unanalyzed_languages(DOTNET_END_LOG, tmp_path) == []


def test_detect_unanalyzed_languages_dotnet_mode_still_reports_extension_languages(tmp_path):
    _touch(tmp_path / "Tiny.Core" / "Greeter.cs")
    _touch(tmp_path / "native" / "interop.cpp")
    assert detect_unanalyzed_languages(DOTNET_END_LOG, tmp_path) == ["C++"]


# --- detect_uncompiled_dotnet_languages (scanner-mode: dotnet) -------------
# The .NET scanner only analyzes what build-command compiled. A .cs/.vb
# file under project-base-dir that no project's FilesToAnalyze lists (a
# second solution the build didn't build, a project whose restore failed
# behind `|| true`, a stray file) was not analyzed.


def _compiled(*paths):
    return {os.path.realpath(p) for p in paths}


def test_detect_uncompiled_dotnet_languages_all_compiled(tmp_path):
    _touch(tmp_path / "App" / "App.cs")
    assert detect_uncompiled_dotnet_languages(tmp_path, _compiled(tmp_path / "App" / "App.cs")) == []


def test_detect_uncompiled_dotnet_languages_reports_stray_csharp(tmp_path):
    _touch(tmp_path / "App" / "App.cs")
    _touch(tmp_path / "Unbuilt" / "Unbuilt.cs")
    assert detect_uncompiled_dotnet_languages(tmp_path, _compiled(tmp_path / "App" / "App.cs")) == ["C#"]


def test_detect_uncompiled_dotnet_languages_reports_stray_vbnet(tmp_path):
    _touch(tmp_path / "Legacy" / "Module1.vb")
    assert detect_uncompiled_dotnet_languages(tmp_path, set()) == ["VB.NET"]


def test_detect_uncompiled_dotnet_languages_ignores_bin_obj_and_sonarqube(tmp_path):
    _touch(tmp_path / "App" / "obj" / "Debug" / "net10.0" / "App.AssemblyInfo.cs")
    _touch(tmp_path / "App" / "bin" / "Debug" / "Gen.cs")
    _touch(tmp_path / ".sonarqube" / "conf" / "x.cs")
    _touch(tmp_path / ".baseline-checkout" / "Old.cs")
    assert detect_uncompiled_dotnet_languages(tmp_path, set()) == []


def test_detect_uncompiled_dotnet_languages_honors_exclusions(tmp_path):
    _touch(tmp_path / "samples" / "Sample.cs")
    assert detect_uncompiled_dotnet_languages(tmp_path, set(), exclude_patterns=["samples/**"]) == []


def test_detect_uncompiled_dotnet_languages_in_git_repo_honors_gitignore(tmp_path):
    _init_repo(tmp_path)
    (tmp_path / ".gitignore").write_text("generated/\n")
    _touch(tmp_path / "generated" / "Gen.cs")
    assert detect_uncompiled_dotnet_languages(tmp_path, set()) == []


def test_detect_unanalyzed_languages_dotnet_mode_reports_uncompiled_csharp(tmp_path):
    _touch(tmp_path / "App" / "App.cs")
    _touch(tmp_path / "Unbuilt" / "Unbuilt.cs")
    _touch(tmp_path / "native" / "interop.cpp")
    result = detect_unanalyzed_languages(
        DOTNET_END_LOG, tmp_path, compiled_dotnet_files=_compiled(tmp_path / "App" / "App.cs")
    )
    assert result == ["C#", "C++"]


def test_detect_unanalyzed_languages_cli_mode_ignores_compiled_files_check(tmp_path):
    # compiled_dotnet_files=None (cli mode): .cs files alone are not
    # reported - the CLI's own log line is the cli-mode signal.
    _touch(tmp_path / "App" / "App.cs")
    assert detect_unanalyzed_languages("", tmp_path) == []


def test_detect_unanalyzed_languages_respects_exclusion_patterns(tmp_path):
    _touch(tmp_path / "vendor" / "lib.cpp")
    result = detect_unanalyzed_languages("", tmp_path, exclude_patterns=["**/vendor/**"])
    assert result == []


# --- load_exclude_patterns: .sastrc/.sastignore read through the same
# safe reader as build_scan_config.py (read_config_file), not a plain open ---


def test_load_exclude_patterns_neither_file_present_is_empty(tmp_path):
    assert load_exclude_patterns(str(tmp_path / ".sastrc"), str(tmp_path / ".sastignore"), str(tmp_path)) == []


def test_load_exclude_patterns_merges_sastrc_and_sastignore(tmp_path):
    (tmp_path / ".sastrc").write_text("exclusions:\n  paths:\n    - vendor/**\n")
    (tmp_path / ".sastignore").write_text("build/\n")
    patterns = load_exclude_patterns(str(tmp_path / ".sastrc"), str(tmp_path / ".sastignore"), str(tmp_path))
    assert "vendor/**" in patterns
    assert "**/build/**" in patterns


def test_load_exclude_patterns_rejects_symlinked_sastrc(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("exclusions:\n  paths:\n    - '**'\n")
    project = tmp_path / "project"
    project.mkdir()
    os.symlink(outside / "secret", project / ".sastrc")
    with pytest.raises(ValueError, match="symbolic link"):
        load_exclude_patterns(str(project / ".sastrc"), str(project / ".sastignore"), str(project))


def test_load_exclude_patterns_rejects_symlinked_sastignore(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "ignore").write_text("**\n")
    project = tmp_path / "project"
    project.mkdir()
    os.symlink(outside / "ignore", project / ".sastignore")
    with pytest.raises(ValueError, match="symbolic link"):
        load_exclude_patterns(str(project / ".sastrc"), str(project / ".sastignore"), str(project))


def test_load_exclude_patterns_rejects_config_outside_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / ".sastrc").write_text("exclusions:\n  paths:\n    - '**'\n")
    with pytest.raises(ValueError, match="outside the project directory"):
        load_exclude_patterns(str(tmp_path / ".sastrc"), str(project / ".sastignore"), str(project))


def test_load_exclude_patterns_rejects_fifo(tmp_path):
    os.mkfifo(tmp_path / ".sastrc")
    with pytest.raises(ValueError, match="not a regular file"):
        load_exclude_patterns(str(tmp_path / ".sastrc"), str(tmp_path / ".sastignore"), str(tmp_path))


def test_load_exclude_patterns_propagates_invalid_sastrc(tmp_path):
    (tmp_path / ".sastrc").write_text("nope: 1\n")
    with pytest.raises(ValueError):
        load_exclude_patterns(str(tmp_path / ".sastrc"), str(tmp_path / ".sastignore"), str(tmp_path))
