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
ProjectGuid and a project with no source files):

- `out/<n>/` is created by the Sonar MSBuild targets for every project
  whose compile starts (the compiler writes `Issues.json` there), but
  `out/<n>/ProjectInfo.xml` is only written once that project's build
  finishes. A numbered directory without ProjectInfo.xml is a project
  that started building and never finished.
- `out/ScannerEngineInput.json` is what `end` handed the scanner engine;
  its `sonar.modules` property lists the ProjectGuid of every project
  actually analyzed. A ProjectInfo.xml that isn't excluded
  (`IsExcluded`) and has files to analyze (a `FilesToAnalyze` result -
  a project with no source files legitimately has none and is dropped)
  but whose ProjectGuid is not in `sonar.modules` was skipped by `end`.
- Belt and braces, the scanner's own skip messages in the build/end
  logs ("... will not be analyzed", "... will not be uploaded").

Known blind spot: a project whose build fails before compilation starts
(e.g. restore or evaluation failure) leaves no trace under `out/` at all.
That case is only caught by the build command's own exit status, which
run_scan.sh already fails on - this check exists for build scripts that
mask a failure, not as a replacement for it.

This is a guard against honest build failures, not against a malicious
build: code run by `build-command` can rewrite anything under
`.sonarqube/` (see the README's trust note).
"""
import argparse
import json
import os
import sys
import xml.etree.ElementTree as ET

PROJECT_INFO_NS = "{http://www.sonarsource.com/msbuild/integration/2015/1}"

# Substrings of the scanner's own "this project is being skipped" lines:
#   end:   WARNING: Duplicate ProjectGuid: "...". The project will not be analyzed. Project file: "..."
#   build: Sonar: The project does not have a valid ProjectGuid. Analysis results for this project will not be uploaded. ...
SKIP_LOG_MARKERS = ("will not be analyzed", "will not be uploaded")


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
    }


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
    """Return the scanner's own skip lines (deduplicated, in order, with
    any leading timestamp/indent stripped)."""
    lines = []
    for raw_line in (log_text or "").splitlines():
        if not any(marker in raw_line for marker in SKIP_LOG_MARKERS):
            continue
        line = raw_line.strip()
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
    """Return a list of human-readable problems (empty when every project
    the build touched was analyzed) - see the module docstring."""
    out_dir = os.fspath(out_dir)
    if not os.path.isdir(out_dir):
        return [f"{out_dir} does not exist - the SonarScanner for .NET produced no analysis output"]

    problems = []
    infos = []
    for entry in sorted(os.listdir(out_dir), key=lambda e: (len(e), e)):
        project_dir = os.path.join(out_dir, entry)
        if not entry.isdigit() or not os.path.isdir(project_dir):
            continue
        info_path = os.path.join(project_dir, "ProjectInfo.xml")
        if not os.path.isfile(info_path):
            hint = _first_compiler_error(os.path.join(project_dir, "Issues.json"))
            problems.append(
                f"out/{entry}: a project started building but did not finish building (no ProjectInfo.xml)"
                + (f" - first compiler error: {hint}" if hint else "")
            )
            continue
        with open(info_path, encoding="utf-8") as f:
            try:
                infos.append(parse_project_info(f.read()))
            except ValueError as e:
                problems.append(f"out/{entry}: {e}")

    modules = read_analyzed_modules(out_dir)
    if modules is None:
        problems.append(
            f"{os.path.join(out_dir, 'ScannerEngineInput.json')} is missing - cannot confirm which projects `end` analyzed"
        )
    elif not modules:
        problems.append("`end` analyzed no project at all (sonar.modules is empty)")
    else:
        for info in infos:
            if info["excluded"] or not info["has_files_to_analyze"]:
                continue
            if info["guid"] not in modules:
                problems.append(
                    f"{info['name']} ({info['full_path']}): not analyzed by `end` "
                    f"(ProjectGuid {info['guid'] or '<none>'} missing from sonar.modules - duplicate or invalid ProjectGuid?)"
                )

    problems.extend(f"scanner log: {line}" for line in find_skipped_log_lines(log_text))
    return problems


def main():  # pragma: no cover - CLI glue over the above, validated live
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sonarqube-out", required=True, help="Path to <project-base-dir>/.sonarqube/out")
    parser.add_argument("--log", action="append", default=[], help="A captured build/end log to check (repeatable)")
    args = parser.parse_args()

    log_text = ""
    for path in args.log:
        with open(path, encoding="utf-8", errors="replace") as f:
            log_text += f.read()

    problems = find_skipped_projects(args.sonarqube_out, log_text)
    if problems:
        print("Not every project was analyzed by the SonarScanner for .NET:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        sys.exit(1)
    print("Every project the build produced was analyzed.", file=sys.stderr)


if __name__ == "__main__":
    main()
