#!/usr/bin/env python3
"""Detect languages present under a scanned project that `sonar-scanner-cli`
(the plain `cli` scan mode, as opposed to a build-integrated scanner) did
not actually analyze - so `run_scan.sh` never reports a clean result for
files it never looked at.

Two independent detection sources, combined:

- Languages with a Community Build plugin that still can't run through the
  plain CLI (C#, VB.NET): the scanner itself logs a fixed-sentence WARN line
  naming the language, e.g.:

    WARN  Your project contains C# files which cannot be analyzed with the
    scanner you are using. To analyze C# or VB.NET, you must use the
    SonarScanner for .NET 5.x or higher, see
    https://redirect.sonarsource.com/doc/install-configure-scanner-msbuild.html

  `parse_scanner_log()` matches that sentence generically (whatever language
  name sits between "contains" and "files which cannot be analyzed"),
  rather than hard-coding "C#" - confirmed live against SonarQube 26.8 (see
  docs/csharp-support-plan.md, "Phase 0 findings").

- Languages with no Community Build plugin at all (C, C++, Objective-C,
  Swift): the scanner doesn't index these files, so it never logs anything
  about them. `detect_extension_languages()` instead walks the project
  looking for a fixed set of file extensions. Deliberately excludes bare
  `.h` - ambiguous between C, C++ and Objective-C headers.

Both sources honor the consumer's `.sastrc` `exclusions.paths` and
`.sastignore` (parsed by build_scan_config.py, reused here rather than
re-implemented) so a file that's excluded from the real scan isn't reported
as "unanalyzed" either - it was never meant to be scanned in the first
place. Path matching against those exclusion globs is a documented
approximation of Sonar's own glob semantics (see `_glob_to_regex` below),
not a byte-for-byte reimplementation - good enough for the common
`**/dir/**` / `dir/**` shapes build_scan_config.py itself produces.
"""
import argparse
import os
import re
import sys

from build_scan_config import parse_sastignore, parse_sastrc

# Matches the scanner's fixed-sentence warning generically, so a future
# Community Build plugin for some other non-analyzable language (today:
# C# and VB.NET) is picked up without a code change here.
LOG_LINE_PATTERN = re.compile(
    r"Your project contains (?P<language>.+?) files which cannot be "
    r"analyzed with the scanner you are using\."
)

# Extensions for languages with no Community Build plugin at all, so the
# scanner never indexes (or warns about) them. ".h" is deliberately absent:
# it's ambiguous between C, C++ and Objective-C, so counting it risks a
# false positive against a repo that's actually all C++.
EXTENSION_LANGUAGES = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".c++": "C++",
    ".m": "Objective-C",
    ".mm": "Objective-C",
    ".swift": "Swift",
}

# Never treated as scan input regardless of .sastrc/.sastignore - the same
# directories run_scan.sh itself cleans or the scanner writes into.
IGNORED_DIR_NAMES = {".git", ".scannerwork", ".sonarqube"}


def parse_scanner_log(log_text):
    """Return the sorted-by-first-appearance, deduplicated list of language
    names named in the scanner's "cannot be analyzed" warning line(s).
    Empty list when the text is empty/None or contains no such line -
    including when it contains some *other*, unrelated WARN line."""
    if not log_text:
        return []
    languages = []
    for match in LOG_LINE_PATTERN.finditer(log_text):
        language = match.group("language").strip()
        if language not in languages:
            languages.append(language)
    return languages


def _glob_to_regex(pattern):
    """Translate one Sonar-style exclusion glob (the shape
    build_scan_config.py produces from .sastrc `exclusions.paths` /
    .sastignore) into a regex anchored to a full relative-path match.

    Documented approximation of Sonar's own glob semantics, not a
    byte-for-byte reimplementation: '**' matches zero or more path
    segments (so '**/vendor/**' matches 'vendor/lib.cpp' at the project
    root too, not just nested), a lone '*' matches any run of
    non-separator characters, and '?' matches exactly one. Good enough for
    the 'dir/**' / '**/dir/**' / '**/*.ext' shapes this action's own config
    parsing ever emits."""
    regex = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern[i : i + 3] == "**/":
            regex.append("(?:.*/)?")
            i += 3
        elif pattern[i : i + 2] == "**":
            regex.append(".*")
            i += 2
        elif pattern[i] == "*":
            regex.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            regex.append("[^/]")
            i += 1
        else:
            regex.append(re.escape(pattern[i]))
            i += 1
    return re.compile(f"^{''.join(regex)}$")


def _path_matches_any(rel_path, patterns):
    return any(_glob_to_regex(pattern).match(rel_path) for pattern in patterns)


def detect_extension_languages(base_dir, exclude_patterns=()):
    """Walk `base_dir` for files whose extension belongs to a language the
    Community Build has no plugin for at all (see EXTENSION_LANGUAGES),
    skipping `.git/`, `.scannerwork/`, `.sonarqube/` and anything matched by
    `exclude_patterns` (Sonar-style exclusion globs, relative to base_dir -
    see `_path_matches_any`). Returns a sorted, deduplicated list of
    language names."""
    base_dir = os.fspath(base_dir)
    found = set()
    for root, dirs, files in os.walk(base_dir):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIR_NAMES]
        for filename in files:
            ext = os.path.splitext(filename)[1].lower()
            language = EXTENSION_LANGUAGES.get(ext)
            if language is None:
                continue
            rel_path = os.path.relpath(os.path.join(root, filename), base_dir).replace(os.sep, "/")
            if _path_matches_any(rel_path, exclude_patterns):
                continue
            found.add(language)
    return sorted(found)


def detect_unanalyzed_languages(scanner_log_text, base_dir, exclude_patterns=()):
    """Combine both detection sources into one sorted, deduplicated list of
    language names the `cli` scan did not analyze. The two sources are
    expected to be disjoint in practice (no Community Build plugin logs the
    C/C++/Objective-C/Swift warning), but the combination dedupes anyway."""
    languages = set(parse_scanner_log(scanner_log_text))
    languages.update(detect_extension_languages(base_dir, exclude_patterns))
    return sorted(languages)


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scanner-log", required=True, help="Path to the captured scanner stdout/stderr log")
    parser.add_argument("--project-base-dir", required=True, help="Path the scanner ran against")
    parser.add_argument("--config", required=True, help="Path to an optional .sastrc file (need not exist)")
    parser.add_argument("--ignore-file", required=True, help="Path to an optional .sastignore file (need not exist)")
    parser.add_argument("--out", required=True, help="Path to write the comma-separated language list to (empty file when none)")
    args = parser.parse_args()

    with open(args.scanner_log, encoding="utf-8", errors="replace") as f:
        log_text = f.read()

    sastrc_text = None
    if os.path.isfile(args.config):
        with open(args.config) as f:
            sastrc_text = f.read()

    sastignore_text = None
    if os.path.isfile(args.ignore_file):
        with open(args.ignore_file) as f:
            sastignore_text = f.read()

    try:
        sastrc = parse_sastrc(sastrc_text or "")
        ignore_patterns = parse_sastignore(sastignore_text or "")
    except ValueError as e:
        raise SystemExit(f"Invalid SAST config: {e}")

    exclude_patterns = sastrc["exclusions"] + ignore_patterns
    languages = detect_unanalyzed_languages(log_text, args.project_base_dir, exclude_patterns)

    with open(args.out, "w") as f:
        f.write(",".join(languages))

    if languages:
        print(f"Languages not analyzed by sonar-scanner-cli: {', '.join(languages)}", file=sys.stderr)


if __name__ == "__main__":
    main()
