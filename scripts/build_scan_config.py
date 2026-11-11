#!/usr/bin/env python3
"""Turn a consumer repo's optional `.sastrc` (YAML) and `.sastignore`
(gitignore-syntax) files into extra sonar-scanner `-D` properties, so
run_scan.sh can pass them straight through to `docker run`.

Both files are optional and independent - either, both, or neither may
exist. Neither existing reproduces today's exact scanner invocation (empty
output). `.sastrc` covers exclusions, coverage/duplication exclusions, and
per-rule issue-ignore criteria; `.sastignore` covers path exclusions only,
using familiar `.gitignore` syntax, and its patterns are merged into the
same `sonar.exclusions` value `.sastrc`'s `exclusions.paths` produces.

`.sastrc` schema:

    exclusions:
      paths: ["**/vendor/**"]              # -> sonar.exclusions
      coverage_paths: ["**/mocks/**"]      # -> sonar.coverage.exclusions
      duplication_paths: ["**/testdata/**"]  # -> sonar.cpd.exclusions
    tests:
      paths: ["src/**/*.test.ts"]          # -> sonar.test.inclusions (see below)
    ignore:
      - rule: "python:S101"
        paths: ["tests/**"]

Every value is validated against this schema; an unrecognized key, wrong
type, or missing required field fails closed (raises ValueError) rather
than silently ignoring the mistake - a consumer's config error must not
look like "noise reduction applied" when it wasn't. There is deliberately
no way to set sonar.host.url/token/projectKey/projectBaseDir/working.directory
or any other property through either file: the schema simply has no field
for them.

`tests.paths` does NOT map onto sonar.tests directly: sonar.tests only
accepts a comma-separated list of directories, not globs, and rejects `*`
fatally - but the whole reason this key exists is to let a caller name
test files a filename heuristic can't catch (e.g. `*.steps.ts`), which
needs a glob. Pointing sonar.tests at a narrower directory than
sonar.sources also has its own failure mode: SonarQube silently drops
everything outside that directory from the scan instead of erroring, so a
caller who lists their test directories ends up with a scan that only
reads those directories. Both are avoided by pointing sonar.sources and
sonar.tests at the same root (the project base dir) and using
sonar.test.inclusions - which is glob-capable - to say which files under
that shared root are tests:

    sonar.sources=.
    sonar.tests=.
    sonar.test.inclusions=<tests.paths, comma-joined; a bare directory
                            entry with no glob character is expanded to
                            "<dir>/**" so it still selects everything
                            under it>

With `--scanner-mode dotnet` (run_scan.sh's `scanner-mode: dotnet`), the
same properties are written as `/d:key=value` tokens for
`dotnet-sonarscanner begin` instead, and `tests.paths` is rejected rather
than translated - see build_dotnet_scan_properties().
"""
import argparse
import os
import sys

import yaml

ALLOWED_TOP_LEVEL_KEYS = {"exclusions", "tests", "ignore"}
ALLOWED_EXCLUSIONS_KEYS = {"paths", "coverage_paths", "duplication_paths"}
ALLOWED_TESTS_KEYS = {"paths"}
ALLOWED_IGNORE_KEYS = {"rule", "paths"}


def _require_list_of_str(value, desc):
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{desc} must be a list of non-empty strings")
    return value


def parse_sastrc(text):
    """Parse and validate `.sastrc` YAML text into:

        {"exclusions": [...], "coverage_exclusions": [...],
         "duplication_exclusions": [...], "tests": [...],
         "ignore": [(rule, path), ...]}

    Empty/blank text is treated as an empty (valid) config. Raises
    ValueError, naming the offending field, on anything that doesn't match
    the schema.
    """
    result = {
        "exclusions": [],
        "coverage_exclusions": [],
        "duplication_exclusions": [],
        "tests": [],
        "ignore": [],
    }
    if not text.strip():
        return result

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f".sastrc is not valid YAML: {e}")

    if data is None:
        return result
    if not isinstance(data, dict):
        raise ValueError(".sastrc must be a YAML mapping at the top level")

    unknown = set(data) - ALLOWED_TOP_LEVEL_KEYS
    if unknown:
        raise ValueError(f".sastrc has unrecognized top-level key(s): {', '.join(sorted(unknown))}")

    exclusions = data.get("exclusions")
    if exclusions is not None:
        if not isinstance(exclusions, dict):
            raise ValueError(".sastrc: 'exclusions' must be a mapping")
        unknown = set(exclusions) - ALLOWED_EXCLUSIONS_KEYS
        if unknown:
            raise ValueError(f".sastrc: 'exclusions' has unrecognized key(s): {', '.join(sorted(unknown))}")
        result["exclusions"] = _require_list_of_str(exclusions.get("paths", []), ".sastrc: exclusions.paths")
        result["coverage_exclusions"] = _require_list_of_str(
            exclusions.get("coverage_paths", []), ".sastrc: exclusions.coverage_paths"
        )
        result["duplication_exclusions"] = _require_list_of_str(
            exclusions.get("duplication_paths", []), ".sastrc: exclusions.duplication_paths"
        )

    tests = data.get("tests")
    if tests is not None:
        if not isinstance(tests, dict):
            raise ValueError(".sastrc: 'tests' must be a mapping")
        unknown = set(tests) - ALLOWED_TESTS_KEYS
        if unknown:
            raise ValueError(f".sastrc: 'tests' has unrecognized key(s): {', '.join(sorted(unknown))}")
        result["tests"] = _require_list_of_str(tests.get("paths", []), ".sastrc: tests.paths")

    ignore = data.get("ignore")
    if ignore is not None:
        if not isinstance(ignore, list):
            raise ValueError(".sastrc: 'ignore' must be a list")
        for i, entry in enumerate(ignore):
            if not isinstance(entry, dict):
                raise ValueError(f".sastrc: ignore[{i}] must be a mapping")
            unknown = set(entry) - ALLOWED_IGNORE_KEYS
            if unknown:
                raise ValueError(f".sastrc: ignore[{i}] has unrecognized key(s): {', '.join(sorted(unknown))}")
            rule = entry.get("rule")
            if not isinstance(rule, str) or not rule:
                raise ValueError(f".sastrc: ignore[{i}] is missing a non-empty 'rule'")
            paths = entry.get("paths")
            if paths is None:
                raise ValueError(f".sastrc: ignore[{i}] is missing 'paths'")
            paths = _require_list_of_str(paths, f".sastrc: ignore[{i}].paths")
            if not paths:
                raise ValueError(f".sastrc: ignore[{i}].paths must not be empty")
            for path in paths:
                result["ignore"].append((rule, path))

    return result


def translate_gitignore_pattern(line):
    """Translate one `.sastignore` (gitignore-syntax) line into the
    Sonar-style exclusion glob(s) that cover it.

    A gitignore pattern matching a directory excludes everything under it;
    since this runs with no filesystem access to tell a file from a
    directory apart, an ordinary (non-dir-only) pattern is translated into
    both its bare form and a recursive '/**' form, so it excludes correctly
    either way. Raises ValueError on a `!negation` line, which has no
    equivalent in sonar.exclusions (no per-pattern re-include).
    """
    if line.startswith("!"):
        raise ValueError(
            f".sastignore: negated pattern {line!r} is not supported "
            "(sonar.exclusions has no per-pattern re-include) - remove it, "
            "or express the exception via .sastrc's 'ignore' criteria instead"
        )

    is_dir_only = line.endswith("/")
    pattern = line[:-1] if is_dir_only else line

    # Per gitignore's own rule: a '/' anywhere but the very end anchors the
    # pattern to the root it's defined at (here, project-base-dir); no such
    # '/' means it can match at any depth.
    anchored = pattern.startswith("/") or "/" in pattern
    if pattern.startswith("/"):
        pattern = pattern[1:]
    if not anchored:
        pattern = f"**/{pattern}"

    if is_dir_only:
        return [f"{pattern}/**"]
    return [pattern, f"{pattern}/**"]


def parse_sastignore(text):
    """Parse `.sastignore` text into a flat list of Sonar-style exclusion
    globs. Comments ('#') and blank lines are ignored, matching gitignore."""
    patterns = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.extend(translate_gitignore_pattern(line))
    return patterns


def build_scan_properties(sastrc_text, sastignore_text, scm_disabled=False):
    """Combine optional `.sastrc` and `.sastignore` text (either may be None
    or empty) into the final ordered list of `key=value` sonar-scanner
    properties. Returns [] when both are absent/empty and scm_disabled is
    False, reproducing today's exact scanner invocation.

    scm_disabled=True adds sonar.scm.disabled=true, for a shallow checkout
    where the SCM sensor would otherwise warn about missing blame data for
    every file (see run_scan.sh's SCM_DISABLED env var)."""
    sastrc = parse_sastrc(sastrc_text or "")
    ignore_patterns = parse_sastignore(sastignore_text or "")

    props = []

    if scm_disabled:
        props.append("sonar.scm.disabled=true")

    exclusions = sastrc["exclusions"] + ignore_patterns
    if exclusions:
        props.append(f"sonar.exclusions={','.join(exclusions)}")
    if sastrc["coverage_exclusions"]:
        props.append(f"sonar.coverage.exclusions={','.join(sastrc['coverage_exclusions'])}")
    if sastrc["duplication_exclusions"]:
        props.append(f"sonar.cpd.exclusions={','.join(sastrc['duplication_exclusions'])}")
    if sastrc["tests"]:
        # sonar.tests only accepts directories, not globs (fatal error
        # otherwise), and pointing it at a directory narrower than
        # sonar.sources makes SonarQube silently scan only that directory.
        # Keep both sources and tests at the shared project root and use
        # the glob-capable sonar.test.inclusions to mark which files under
        # it are tests - see the module docstring.
        inclusions = [path if any(c in path for c in "*?") else f"{path}/**" for path in sastrc["tests"]]
        props.append("sonar.sources=.")
        props.append("sonar.tests=.")
        props.append(f"sonar.test.inclusions={','.join(inclusions)}")

    if sastrc["ignore"]:
        ids = [f"e{i + 1}" for i in range(len(sastrc["ignore"]))]
        props.append(f"sonar.issue.ignore.multicriteria={','.join(ids)}")
        for criterion_id, (rule, path) in zip(ids, sastrc["ignore"]):
            props.append(f"sonar.issue.ignore.multicriteria.{criterion_id}.ruleKey={rule}")
            props.append(f"sonar.issue.ignore.multicriteria.{criterion_id}.resourceKey={path}")

    return props


def build_dotnet_scan_properties(sastrc_text, sastignore_text, scm_disabled=False):
    """Same translation as build_scan_properties(), for `scanner-mode:
    dotnet` (SonarScanner for .NET `begin`), with two differences found
    in the Phase 0 spike (docs/csharp-support-plan.md):

    - `.sastrc` `tests.paths` is rejected (ValueError naming the field)
      rather than translated: the .NET scanner ignores sonar.sources and
      sonar.tests (test projects are decided by MSBuild), so the
      translation above would silently do nothing - and a config error
      must not look like noise reduction applied.
    - DOTNET_FORBIDDEN_KEYS are never emitted, checked here as defense in
      depth rather than trusted to the shared translation: sonar.sources
      and sonar.tests are ignored by `begin`, and sonar.working.directory
      makes it exit 1.

    Everything else (exclusions, cpd/coverage exclusions,
    sonar.issue.ignore.multicriteria.*, sonar.scm.disabled) carries over
    unchanged; the spike verified each is honoured via `/d:`."""
    sastrc = parse_sastrc(sastrc_text or "")
    if sastrc["tests"]:
        raise ValueError(
            ".sastrc: tests.paths is not supported with scanner-mode: dotnet - the SonarScanner "
            "for .NET ignores sonar.sources/sonar.tests and decides which projects are tests "
            "from MSBuild (e.g. a test SDK reference or <SonarQubeTestProject>true</SonarQubeTestProject>); "
            "remove tests.paths from .sastrc"
        )
    props = build_scan_properties(sastrc_text, sastignore_text, scm_disabled=scm_disabled)
    for prop in props:
        key = prop.split("=", 1)[0]
        if key in DOTNET_FORBIDDEN_KEYS:
            raise ValueError(f"{key} must never be passed to the SonarScanner for .NET")
    return props


# Properties `dotnet-sonarscanner begin` must never receive - see
# build_dotnet_scan_properties().
DOTNET_FORBIDDEN_KEYS = frozenset({"sonar.sources", "sonar.tests", "sonar.working.directory"})

SCANNER_ARG_PREFIXES = {
    "cli": "-D",  # sonar-scanner-cli: -Dkey=value
    "dotnet": "/d:",  # dotnet-sonarscanner begin: /d:key=value
}


def format_scanner_args(props, scanner_mode):
    """Turn `key=value` properties into the command-line tokens the given
    scanner mode's scanner takes."""
    try:
        prefix = SCANNER_ARG_PREFIXES[scanner_mode]
    except KeyError:
        raise ValueError(f"unknown scanner mode {scanner_mode!r} (expected one of: {', '.join(SCANNER_ARG_PREFIXES)})")
    return [f"{prefix}{prop}" for prop in props]


def main():  # pragma: no cover - CLI glue, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to an optional .sastrc file (need not exist)")
    parser.add_argument("--ignore-file", required=True, help="Path to an optional .sastignore file (need not exist)")
    parser.add_argument(
        "--out",
        required=True,
        help="Path to write NUL-delimited -Dkey=value (cli) or /d:key=value (dotnet) tokens to",
    )
    parser.add_argument(
        "--scm-disabled",
        action="store_true",
        help="Add sonar.scm.disabled=true, for a shallow checkout with no blame data available",
    )
    parser.add_argument(
        "--scanner-mode",
        choices=sorted(SCANNER_ARG_PREFIXES),
        default="cli",
        help="Which scanner the tokens are for (default: cli, sonar-scanner-cli)",
    )
    args = parser.parse_args()

    sastrc_text = None
    if os.path.isfile(args.config):
        with open(args.config) as f:
            sastrc_text = f.read()

    sastignore_text = None
    if os.path.isfile(args.ignore_file):
        with open(args.ignore_file) as f:
            sastignore_text = f.read()

    build = build_dotnet_scan_properties if args.scanner_mode == "dotnet" else build_scan_properties
    try:
        props = build(sastrc_text, sastignore_text, scm_disabled=args.scm_disabled)
    except ValueError as e:
        raise SystemExit(f"Invalid SAST config: {e}")

    with open(args.out, "wb") as f:
        for token in format_scanner_args(props, args.scanner_mode):
            f.write(token.encode())
            f.write(b"\0")

    if sastrc_text is not None:
        print(f"Applied SAST config from {args.config}", file=sys.stderr)
    if sastignore_text is not None:
        print(f"Applied SAST ignore patterns from {args.ignore_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
