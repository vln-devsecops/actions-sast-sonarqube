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
"""
import argparse
import os
import sys

import yaml

ALLOWED_TOP_LEVEL_KEYS = {"exclusions", "ignore"}
ALLOWED_EXCLUSIONS_KEYS = {"paths", "coverage_paths", "duplication_paths"}
ALLOWED_IGNORE_KEYS = {"rule", "paths"}


def _require_list_of_str(value, desc):
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{desc} must be a list of non-empty strings")
    return value


def parse_sastrc(text):
    """Parse and validate `.sastrc` YAML text into:

        {"exclusions": [...], "coverage_exclusions": [...],
         "duplication_exclusions": [...], "ignore": [(rule, path), ...]}

    Empty/blank text is treated as an empty (valid) config. Raises
    ValueError, naming the offending field, on anything that doesn't match
    the schema.
    """
    result = {"exclusions": [], "coverage_exclusions": [], "duplication_exclusions": [], "ignore": []}
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


def build_scan_properties(sastrc_text, sastignore_text):
    """Combine optional `.sastrc` and `.sastignore` text (either may be None
    or empty) into the final ordered list of `key=value` sonar-scanner
    properties. Returns [] when both are absent/empty, reproducing today's
    exact scanner invocation."""
    sastrc = parse_sastrc(sastrc_text or "")
    ignore_patterns = parse_sastignore(sastignore_text or "")

    props = []

    exclusions = sastrc["exclusions"] + ignore_patterns
    if exclusions:
        props.append(f"sonar.exclusions={','.join(exclusions)}")
    if sastrc["coverage_exclusions"]:
        props.append(f"sonar.coverage.exclusions={','.join(sastrc['coverage_exclusions'])}")
    if sastrc["duplication_exclusions"]:
        props.append(f"sonar.cpd.exclusions={','.join(sastrc['duplication_exclusions'])}")

    if sastrc["ignore"]:
        ids = [f"e{i + 1}" for i in range(len(sastrc["ignore"]))]
        props.append(f"sonar.issue.ignore.multicriteria={','.join(ids)}")
        for criterion_id, (rule, path) in zip(ids, sastrc["ignore"]):
            props.append(f"sonar.issue.ignore.multicriteria.{criterion_id}.ruleKey={rule}")
            props.append(f"sonar.issue.ignore.multicriteria.{criterion_id}.resourceKey={path}")

    return props


def main():  # pragma: no cover - CLI glue, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="Path to an optional .sastrc file (need not exist)")
    parser.add_argument("--ignore-file", required=True, help="Path to an optional .sastignore file (need not exist)")
    parser.add_argument("--out", required=True, help="Path to write NUL-delimited -Dkey=value tokens to")
    args = parser.parse_args()

    sastrc_text = None
    if os.path.isfile(args.config):
        with open(args.config) as f:
            sastrc_text = f.read()

    sastignore_text = None
    if os.path.isfile(args.ignore_file):
        with open(args.ignore_file) as f:
            sastignore_text = f.read()

    try:
        props = build_scan_properties(sastrc_text, sastignore_text)
    except ValueError as e:
        raise SystemExit(f"Invalid SAST config: {e}")

    with open(args.out, "wb") as f:
        for prop in props:
            f.write(f"-D{prop}".encode())
            f.write(b"\0")

    if sastrc_text is not None:
        print(f"Applied SAST config from {args.config}", file=sys.stderr)
    if sastignore_text is not None:
        print(f"Applied SAST ignore patterns from {args.ignore_file}", file=sys.stderr)


if __name__ == "__main__":
    main()
