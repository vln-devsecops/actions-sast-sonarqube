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

Both files are read only as regular files inside project-base-dir: a path
that resolves outside it, or that is (or passes through) a symbolic link
below it, fails closed - see read_config_file().

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
than translated - see build_dotnet_scan_properties(). Allowlisted
`sonar-project.properties` keys are passed on the same way, except
`sonar.sources`/`sonar.tests`, which the SonarScanner for .NET ignores:
those are dropped with a warning - see filter_dotnet_project_properties().
"""
import argparse
import os
import stat
import sys

import yaml

from project_properties import filter_project_properties, format_dropped_keys, parse_properties

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
        # By this point the file has already passed read_config_file()'s
        # checks (a regular file inside project-base-dir, reached through
        # no symbolic link), so it's just the repo's own file - PyYAML's
        # own message, which quotes the offending line, is safe and useful
        # to include here.
        raise ValueError(f".sastrc is not valid YAML: {e}") from None

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


def build_scan_properties(sastrc_text, sastignore_text, scm_disabled=False, project_properties=None):
    """Combine optional `.sastrc` and `.sastignore` text (either may be None
    or empty) into the final ordered list of `key=value` sonar-scanner
    properties. Returns [] when both are absent/empty and scm_disabled is
    False, reproducing today's exact scanner invocation.

    scm_disabled=True adds sonar.scm.disabled=true, for a shallow checkout
    where the SCM sensor would otherwise warn about missing blame data for
    every file (see run_scan.sh's SCM_DISABLED env var).

    project_properties: the already-filtered allowlisted keys from the
    consumer's sonar-project.properties (see project_properties.py). They
    come first; any key `.sastrc`/`.sastignore` also sets is taken from
    those instead, so every key is emitted exactly once.

    Raises ValueError if any value holds a `${...}` placeholder, which the
    scanner would otherwise expand (including `${env.NAME}`)."""
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

    sast_keys = {prop.split("=", 1)[0] for prop in props}
    project_props = [
        f"{key}={value}" for key, value in (project_properties or {}).items() if key not in sast_keys
    ]
    merged = project_props + props
    for prop in merged:
        key, value = prop.split("=", 1)
        if "${" in value:
            raise ValueError(f"{key} must not contain a ${{...}} placeholder")
    return merged


def build_dotnet_scan_properties(sastrc_text, sastignore_text, scm_disabled=False, project_properties=None):
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
    unchanged; the spike verified each is honoured via `/d:`.

    project_properties: allowlisted sonar-project.properties keys, as for
    build_scan_properties(), already passed through
    filter_dotnet_project_properties() - a sonar.sources/sonar.tests left
    in them is a ValueError here, not silently forwarded."""
    sastrc = parse_sastrc(sastrc_text or "")
    if sastrc["tests"]:
        raise ValueError(
            ".sastrc: tests.paths is not supported with scanner-mode: dotnet - the SonarScanner "
            "for .NET ignores sonar.sources/sonar.tests and decides which projects are tests "
            "from MSBuild (e.g. a test SDK reference or <SonarQubeTestProject>true</SonarQubeTestProject>); "
            "remove tests.paths from .sastrc"
        )
    props = build_scan_properties(
        sastrc_text, sastignore_text, scm_disabled=scm_disabled, project_properties=project_properties
    )
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


# Allowlisted sonar-project.properties keys (project_properties.py) the
# SonarScanner for .NET does not support: it ignores both (with a warning)
# and takes sources/tests from MSBuild instead.
DOTNET_UNSUPPORTED_PROJECT_KEYS = frozenset({"sonar.sources", "sonar.tests"})


def filter_dotnet_project_properties(project_properties):
    """Split already-allowlisted sonar-project.properties keys into
    (kept, dropped_keys) for `scanner-mode: dotnet`.

    The SonarScanner for .NET never reads sonar-project.properties itself
    (verified live against 12.1.0: a multicriteria ignore in the file had
    no effect), so without this the file would silently apply in cli mode
    but not in dotnet mode. Every allowlisted key is passed on as `/d:`,
    except DOTNET_UNSUPPORTED_PROJECT_KEYS: dropping those only ever widens
    what is analyzed (never hides a finding), so they are dropped with a
    warning rather than failing the scan - unlike `.sastrc` `tests.paths`,
    which is this action's own setting and fails closed."""
    kept = {}
    dropped = []
    for key, value in (project_properties or {}).items():
        if key in DOTNET_UNSUPPORTED_PROJECT_KEYS:
            dropped.append(key)
        else:
            kept[key] = value
    return kept, dropped


def _is_within(path, directory):
    return os.path.commonpath([path, directory]) == directory


def read_config_file(path, project_base_dir, encoding=None, errors=None):
    """Read an optional config file that must live inside project_base_dir.

    Returns the file's text, or None if it does not exist. Fails closed
    (raises ValueError, without including any file content) if the path
    resolves outside project_base_dir, if any component of it below
    project_base_dir is a symbolic link, or if it is not a regular file.
    project_base_dir itself may be reached through a symbolic link; only
    the components below it are checked.
    """
    base_abs = os.path.abspath(project_base_dir)
    base_real = os.path.realpath(project_base_dir)
    rel = os.path.relpath(os.path.abspath(path), base_abs)
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        raise ValueError(f"{path} is outside the project directory {project_base_dir}")

    candidate = base_real
    if rel != os.curdir:
        for part in rel.split(os.sep):
            candidate = os.path.join(candidate, part)
            if os.path.islink(candidate):
                raise ValueError(
                    f"{path} is or is under a symbolic link; config files must be "
                    "regular files inside the project directory"
                )
            if not os.path.lexists(candidate):
                return None

    if not _is_within(os.path.realpath(candidate), base_real):  # pragma: no cover - defence in depth
        raise ValueError(f"{path} is outside the project directory {project_base_dir}")

    # O_NOFOLLOW: never follow a link swapped in after the checks above.
    # O_NONBLOCK: don't hang opening a FIFO before fstat can reject it.
    fd = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"{path} is not a regular file")
    with os.fdopen(fd, "r", encoding=encoding, errors=errors) as f:
        return f.read()


def main():  # pragma: no cover - CLI glue, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to an optional .sastrc file (need not exist)")
    parser.add_argument("--ignore-file", required=True, help="Path to an optional .sastignore file (need not exist)")
    parser.add_argument(
        "--project-properties",
        help=(
            "Path to an optional sonar-project.properties file (need not exist); only "
            "allowlisted keys are passed on, the rest are dropped with a warning"
        ),
    )
    parser.add_argument(
        "--project-base-dir",
        default=".",
        help="Directory both config files must be inside (default: current directory)",
    )
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

    try:
        sastrc_text = read_config_file(args.config, args.project_base_dir)
        sastignore_text = read_config_file(args.ignore_file, args.project_base_dir)
        project_properties, dropped_keys = {}, []
        if args.project_properties:
            # Same decoding as the scanner: UTF-8, malformed bytes replaced.
            properties_text = read_config_file(
                args.project_properties, args.project_base_dir, encoding="utf-8", errors="replace"
            )
            if properties_text is not None:
                project_properties, dropped_keys = filter_project_properties(parse_properties(properties_text))
        dotnet_dropped_keys = []
        if args.scanner_mode == "dotnet":
            project_properties, dotnet_dropped_keys = filter_dotnet_project_properties(project_properties)
        build = build_dotnet_scan_properties if args.scanner_mode == "dotnet" else build_scan_properties
        props = build(
            sastrc_text,
            sastignore_text,
            scm_disabled=args.scm_disabled,
            project_properties=project_properties,
        )
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
    if project_properties:
        print(f"Applied sonar-project.properties keys: {', '.join(project_properties)}", file=sys.stderr)
    if dropped_keys:
        # Key names are consumer-supplied text: sanitised before printing,
        # and values are never printed.
        print(
            "::warning::sonar-project.properties keys ignored (not in the supported list): "
            + format_dropped_keys(dropped_keys)
        )
    if dotnet_dropped_keys:
        print(
            "::warning::sonar-project.properties keys ignored with scanner-mode: dotnet (the "
            "SonarScanner for .NET takes sources and tests from MSBuild): "
            + format_dropped_keys(dotnet_dropped_keys)
        )


if __name__ == "__main__":
    main()
