#!/usr/bin/env python3
"""Fail a `scanner-mode: dotnet` scan when any project the build touched
was not actually analyzed - run by run_scan.sh after `dotnet-sonarscanner
end` has exited 0.

`end` exiting 0 with "ANALYSIS SUCCESSFUL" is not enough: the Phase 0
spike (docs/csharp-support-plan.md) showed it does exactly that when one
project of a solution failed to build (the build script swallowed the
failure) and when projects share an invalid/duplicate ProjectGuid - the
skipped projects simply vanish from the results, which then look clean.

Signal, from the files the scanner itself writes under
`<project-base-dir>/.sonarqube/out/` (confirmed against dotnet-sonarscanner
12.1.0 + SonarQube 26.8 on a full build, a partial build, a duplicate
ProjectGuid, a project with no source files and a
`<SonarQubeExclude>true</SonarQubeExclude>` project):

- `out/<n>/` is created by the Sonar MSBuild targets for every project
  whose compile starts (the compiler writes `Issues.json` there), but
  `out/<n>/ProjectInfo.xml` is only written once that project's build
  finishes. A numbered directory without ProjectInfo.xml is a project
  that started building and never finished.
- `out/ScannerEngineInput.json` is what `end` handed the scanner engine;
  its `sonar.modules` property lists the ProjectGuid of every project
  actually analyzed. A ProjectInfo.xml that isn't excluded
  (`IsExcluded` - set by `<SonarQubeExclude>`, a deliberate, documented
  exclusion) and has files to analyze (a `FilesToAnalyze` result - a
  project with no source files legitimately has none and is dropped)
  but whose ProjectGuid is not in `sonar.modules` was skipped by `end`.
- Belt and braces, the scanner's own ProjectGuid skip messages in the
  build/end logs. Only those: `end` also logs "The exclude flag has been
  set so the project will not be analyzed" for a `<SonarQubeExclude>`
  project, which must not fail the scan.

Each problem carries a reason code (`Problem.code`) that run_scan.sh
surfaces as the action's `failure-reason` output.

Known blind spot: a project whose build fails before compilation starts
(e.g. restore or evaluation failure) leaves no trace under `out/` at all.
That case is only caught by the build command's own exit status, which
run_scan.sh already fails on, and - if a build script masks it - by
detect_unanalyzed_languages.py reporting its .cs files as unanalyzed C#
(read_compiled_files() below lists what the build did compile).

This is a guard against honest build failures, not against a malicious
build: code run by `build-command` can rewrite anything under
`.sonarqube/` (see the README's trust note).
"""
import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET

PROJECT_INFO_NS = "{http://www.sonarsource.com/msbuild/integration/2015/1}"

# The scanner's own ProjectGuid skip lines - both must mention ProjectGuid
# AND one of the skip phrases:
#   end:   WARNING: Duplicate ProjectGuid: "...". The project will not be analyzed. Project file: "..."
#   build: Sonar: The project does not have a valid ProjectGuid. Analysis results for this project will not be uploaded. ...
SKIP_LOG_MARKERS = ("will not be analyzed", "will not be uploaded")
SKIP_LOG_REQUIRED = "ProjectGuid"

# "21:03:51.291  " - the timestamp the .NET scanner prefixes its own lines with.
_LOG_TIMESTAMP = re.compile(r"^\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s+")

# Reason codes (see run_scan.sh's failure-reason output).
PROJECT_NOT_BUILT = "project-not-built"
PROJECT_NOT_ANALYZED = "project-not-analyzed"
NO_ANALYSIS_OUTPUT = "no-analysis-output"


class Problem(str):
    """A human-readable problem line that also carries its reason code."""

    def __new__(cls, code, message):
        problem = super().__new__(cls, message)
        problem.code = code
        return problem


def reason_codes(problems):
    """Comma-joined, sorted, de-duplicated reason codes ("" when none)."""
    return ",".join(sorted({p.code for p in problems}))


def parse_project_info(xml_text):
    """Read the fields this check needs from one ProjectInfo.xml. Raises
    ValueError on malformed XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise ValueError(f"malformed ProjectInfo.xml: {e}")

    def text(tag):
        element = root.find(f"{PROJECT_INFO_NS}{tag}")
        return (element.text or "").strip() if element is not None else ""

    files_to_analyze = root.find(
        f"{PROJECT_INFO_NS}AnalysisResultFiles/{PROJECT_INFO_NS}AnalysisResultFile[@Id='FilesToAnalyze']"
    )
    return {
        "name": text("ProjectName"),
        "guid": text("ProjectGuid").upper(),
        "full_path": text("FullPath"),
        "excluded": text("IsExcluded").lower() == "true",
        "has_files_to_analyze": files_to_analyze is not None,
        "files_to_analyze": files_to_analyze.get("Location") if files_to_analyze is not None else None,
    }


def _project_dirs(out_dir):
    """Yield (entry, path) for each numbered per-project dir under out/."""
    for entry in sorted(os.listdir(out_dir), key=lambda e: (len(e), e)):
        project_dir = os.path.join(out_dir, entry)
        if entry.isdigit() and os.path.isdir(project_dir):
            yield entry, project_dir


def read_compiled_files(out_dir):
    """Return the set of real paths of every source file any project's
    `FilesToAnalyze` lists - i.e. what the build compiled, including
    `<SonarQubeExclude>` projects (deliberately excluded, still built).
    Missing/malformed entries contribute nothing."""
    out_dir = os.fspath(out_dir)
    if not os.path.isdir(out_dir):
        return set()
    compiled = set()
    for _, project_dir in _project_dirs(out_dir):
        info_path = os.path.join(project_dir, "ProjectInfo.xml")
        try:
            with open(info_path, encoding="utf-8") as f:
                listing = parse_project_info(f.read())["files_to_analyze"]
            if not listing:
                continue
            # The scanner writes this file with a UTF-8 BOM.
            with open(listing, encoding="utf-8-sig") as f:
                compiled.update(os.path.realpath(line.strip()) for line in f if line.strip())
        except (OSError, ValueError):
            continue
    return compiled


def read_analyzed_modules(out_dir):
    """Return the set of (upper-case) ProjectGuids in `sonar.modules` from
    `out/ScannerEngineInput.json`, or None if `end` didn't write it."""
    path = os.path.join(out_dir, "ScannerEngineInput.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for prop in data.get("scannerProperties", []):
        if prop.get("key") == "sonar.modules":
            return {guid.strip().upper() for guid in str(prop.get("value", "")).split(",") if guid.strip()}
    return set()


def find_skipped_log_lines(log_text):
    """Return the scanner's own ProjectGuid skip lines (deduplicated, in
    order, with any leading indent, timestamp or text before a
    WARNING:/Sonar: prefix stripped)."""
    lines = []
    for raw_line in (log_text or "").splitlines():
        if SKIP_LOG_REQUIRED not in raw_line or not any(marker in raw_line for marker in SKIP_LOG_MARKERS):
            continue
        line = _LOG_TIMESTAMP.sub("", raw_line.strip())
        for start in ("WARNING:", "Sonar:"):
            index = line.find(start)
            if index > 0:
                line = line[index:]
                break
        if line not in lines:
            lines.append(line)
    return lines


def _first_compiler_error(issues_path):
    """Best-effort hint for a project that didn't finish building: the
    first error-level result in its Issues.json (SARIF 1.0)."""
    try:
        with open(issues_path, encoding="utf-8") as f:
            sarif = json.load(f)
        for run in sarif.get("runs", []):
            for result in run.get("results", []):
                if result.get("level") != "error":
                    continue
                uri = ""
                for location in result.get("locations", []):
                    uri = location.get("resultFile", {}).get("uri", "")
                    if uri:
                        break
                return f"{result.get('ruleId', '?')} in {uri.removeprefix('file://')}".strip()
    except (OSError, ValueError, AttributeError):
        return None
    return None


def find_skipped_projects(out_dir, log_text):
    """Return a list of Problem (empty when every project the build
    touched was analyzed) - see the module docstring."""
    out_dir = os.fspath(out_dir)
    if not os.path.isdir(out_dir):
        return [Problem(NO_ANALYSIS_OUTPUT, f"{out_dir} does not exist - the SonarScanner for .NET produced no analysis output")]

    problems = []
    infos = []
    for entry, project_dir in _project_dirs(out_dir):
        info_path = os.path.join(project_dir, "ProjectInfo.xml")
        if not os.path.isfile(info_path):
            hint = _first_compiler_error(os.path.join(project_dir, "Issues.json"))
            problems.append(
                Problem(
                    PROJECT_NOT_BUILT,
                    f"out/{entry}: a project started building but did not finish building (no ProjectInfo.xml)"
                    + (f" - first compiler error: {hint}" if hint else ""),
                )
            )
            continue
        with open(info_path, encoding="utf-8") as f:
            try:
                infos.append(parse_project_info(f.read()))
            except ValueError as e:
                problems.append(Problem(PROJECT_NOT_ANALYZED, f"out/{entry}: {e}"))

    modules = read_analyzed_modules(out_dir)
    if modules is None:
        problems.append(
            Problem(
                NO_ANALYSIS_OUTPUT,
                f"{os.path.join(out_dir, 'ScannerEngineInput.json')} is missing - cannot confirm which projects `end` analyzed",
            )
        )
    elif not modules:
        problems.append(Problem(NO_ANALYSIS_OUTPUT, "`end` analyzed no project at all (sonar.modules is empty)"))
    else:
        for info in infos:
            if info["excluded"] or not info["has_files_to_analyze"]:
                continue
            if info["guid"] not in modules:
                problems.append(
                    Problem(
                        PROJECT_NOT_ANALYZED,
                        f"{info['name']} ({info['full_path']}): not analyzed by `end` "
                        f"(ProjectGuid {info['guid'] or '<none>'} missing from sonar.modules - duplicate or invalid ProjectGuid?)",
                    )
                )

    problems.extend(Problem(PROJECT_NOT_ANALYZED, f"scanner log: {line}") for line in find_skipped_log_lines(log_text))
    return problems


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sonarqube-out", required=True, help="Path to <project-base-dir>/.sonarqube/out")
    parser.add_argument("--log", action="append", default=[], help="A captured build/end log to check (repeatable)")
    parser.add_argument("--reason-out", help="Path to write the comma-separated reason codes to (empty when none)")
    args = parser.parse_args()

    log_text = ""
    for path in args.log:
        with open(path, encoding="utf-8", errors="replace") as f:
            log_text += f.read()

    problems = find_skipped_projects(args.sonarqube_out, log_text)
    if args.reason_out:
        with open(args.reason_out, "w") as f:
            f.write(reason_codes(problems))
    if problems:
        print("Not every project was analyzed by the SonarScanner for .NET:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)
    print("Every project the build produced was analyzed.", file=sys.stderr)


if __name__ == "__main__":
    main()
