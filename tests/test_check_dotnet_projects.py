import json
import os

import pytest

from check_dotnet_projects import (
    find_skipped_log_lines,
    find_skipped_projects,
    parse_project_info,
    read_analyzed_modules,
    read_compiled_files,
    reason_codes,
)

# Shapes below are copied from real dotnet-sonarscanner 12.1.0 output
# against SonarQube 26.8 (full build, partial build with the failure
# masked, duplicate ProjectGuid, a project with no source files), trimmed
# to the elements this module reads.

CORE_GUID = "742e96f3-4aee-28a3-3120-ce8630155aa7"
TESTS_GUID = "467f10aa-28ab-8c29-d665-51bb75b75902"


def project_info_xml(name, guid, excluded=False, files_to_analyze=True, files_location="/w/.sonarqube/conf/0/FilesToAnalyze.txt"):
    files = (
        '<AnalysisResultFiles>\n'
        f'    <AnalysisResultFile Id="FilesToAnalyze" Location="{files_location}" />\n'
        '  </AnalysisResultFiles>'
        if files_to_analyze
        else "<AnalysisResultFiles />"
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ProjectInfo xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema" xmlns="http://www.sonarsource.com/msbuild/integration/2015/1">
  <ProjectName>{name}</ProjectName>
  <ProjectLanguage>C#</ProjectLanguage>
  <ProjectType>Product</ProjectType>
  <ProjectGuid>{guid}</ProjectGuid>
  <FullPath>/w/{name}/{name}.csproj</FullPath>
  <IsExcluded>{"true" if excluded else "false"}</IsExcluded>
  {files}
</ProjectInfo>"""


def write_engine_input(out_dir, modules):
    (out_dir / "ScannerEngineInput.json").write_text(
        json.dumps(
            {
                "scannerProperties": [
                    {"key": "sonar.projectKey", "value": "k"},
                    {"key": "sonar.modules", "value": ",".join(g.upper() for g in modules)},
                ]
            }
        )
    )


def write_project(out_dir, index, xml=None, issues=None):
    project_dir = out_dir / str(index)
    project_dir.mkdir(parents=True)
    if xml is not None:
        (project_dir / "ProjectInfo.xml").write_text(xml)
    if issues is not None:
        (project_dir / "Issues.json").write_text(json.dumps(issues))
    return project_dir


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / ".sonarqube" / "out"
    d.mkdir(parents=True)
    return d


# --- parse_project_info ------------------------------------------------------


def test_parse_project_info_reads_the_fields_it_needs():
    info = parse_project_info(project_info_xml("Tiny.Core", CORE_GUID))
    assert info == {
        "name": "Tiny.Core",
        "guid": CORE_GUID.upper(),
        "full_path": "/w/Tiny.Core/Tiny.Core.csproj",
        "excluded": False,
        "has_files_to_analyze": True,
        "files_to_analyze": "/w/.sonarqube/conf/0/FilesToAnalyze.txt",
    }


def test_parse_project_info_excluded_and_no_files():
    info = parse_project_info(project_info_xml("Empty", CORE_GUID, excluded=True, files_to_analyze=False))
    assert info["excluded"] is True
    assert info["has_files_to_analyze"] is False


def test_parse_project_info_rejects_malformed_xml():
    with pytest.raises(ValueError, match="ProjectInfo"):
        parse_project_info("<ProjectInfo>")


# --- read_analyzed_modules ---------------------------------------------------


def test_read_analyzed_modules_returns_uppercase_guids(out_dir):
    write_engine_input(out_dir, [CORE_GUID, TESTS_GUID])
    assert read_analyzed_modules(out_dir) == {CORE_GUID.upper(), TESTS_GUID.upper()}


def test_read_analyzed_modules_missing_file_is_none(out_dir):
    assert read_analyzed_modules(out_dir) is None


def test_read_analyzed_modules_empty_modules_value(out_dir):
    write_engine_input(out_dir, [])
    assert read_analyzed_modules(out_dir) == set()


def test_read_analyzed_modules_without_a_modules_key(out_dir):
    (out_dir / "ScannerEngineInput.json").write_text(json.dumps({"scannerProperties": []}))
    assert read_analyzed_modules(out_dir) == set()


# --- find_skipped_log_lines ----------------------------------------------------


def test_find_skipped_log_lines_duplicate_project_guid():
    # Verbatim from `end` against a solution whose two projects share a
    # ProjectGuid (fhcs-simulator in the Phase 0 spike).
    log = (
        "15:24:32.400  INFO: Load project settings\n"
        '15:24:32.480  WARNING: Duplicate ProjectGuid: "11111111-2222-3333-4444-555555555555". '
        'The project will not be analyzed. Project file: "/w/Tiny.Core/Tiny.Core.csproj"\n'
        "15:24:33.000  INFO: ANALYSIS SUCCESSFUL\n"
    )
    assert find_skipped_log_lines(log) == [
        'WARNING: Duplicate ProjectGuid: "11111111-2222-3333-4444-555555555555". '
        'The project will not be analyzed. Project file: "/w/Tiny.Core/Tiny.Core.csproj"'
    ]


def test_find_skipped_log_lines_invalid_project_guid_from_the_build():
    log = (
        "  Sonar: The project does not have a valid ProjectGuid. Analysis results for this project "
        "will not be uploaded. Project file: /w/A/A.csproj\n"
    )
    assert len(find_skipped_log_lines(log)) == 1


def test_find_skipped_log_lines_clean_log():
    assert find_skipped_log_lines("INFO: ANALYSIS SUCCESSFUL\n") == []
    assert find_skipped_log_lines("") == []
    assert find_skipped_log_lines(None) == []


def test_find_skipped_log_lines_dedupes():
    line = 'WARNING: Duplicate ProjectGuid: "0". The project will not be analyzed. Project file: x'
    assert find_skipped_log_lines(f"{line}\n{line}\n") == [line]


# Verbatim from `end` (dotnet-sonarscanner 12.1.0) for a project with
# <SonarQubeExclude>true</SonarQubeExclude> - Sonar's documented
# per-project exclusion. Deliberate, so it must never fail the scan.
EXCLUDE_FLAG_LINE = (
    "21:03:51.291  The exclude flag has been set so the project will not be analyzed. "
    "Project file: /w/Excluded/Excluded.csproj"
)


def test_find_skipped_log_lines_ignores_the_sonarqube_exclude_flag():
    assert find_skipped_log_lines(EXCLUDE_FLAG_LINE + "\n") == []


def test_find_skipped_log_lines_only_matches_project_guid_skips():
    assert find_skipped_log_lines("WARNING: Some other reason. The project will not be analyzed.\n") == []


def test_find_skipped_log_lines_strips_an_msbuild_node_prefix():
    log = "1>Sonar: The project does not have a valid ProjectGuid. Analysis results for this project will not be uploaded.\n"
    assert find_skipped_log_lines(log) == [
        "Sonar: The project does not have a valid ProjectGuid. Analysis results for this project will not be uploaded."
    ]


def test_find_skipped_log_lines_strips_a_bare_timestamp():
    # A skip line with neither a WARNING: nor a Sonar: prefix still loses
    # its leading timestamp.
    log = "21:03:51.291  The project has an invalid ProjectGuid and will not be analyzed. Project file: x\n"
    assert find_skipped_log_lines(log) == [
        "The project has an invalid ProjectGuid and will not be analyzed. Project file: x"
    ]


# --- find_skipped_projects -----------------------------------------------------


def test_full_build_has_no_skipped_projects(out_dir):
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    write_project(out_dir, 1, project_info_xml("Tiny.Core.Tests", TESTS_GUID))
    write_engine_input(out_dir, [CORE_GUID, TESTS_GUID])
    assert find_skipped_projects(out_dir, "INFO: ANALYSIS SUCCESSFUL") == []


def test_partial_build_project_without_project_info_is_skipped(out_dir):
    # A project whose compile started (the Sonar targets created out/<n>/
    # and the compiler wrote Issues.json) but failed never gets a
    # ProjectInfo.xml - and `end` still exits 0 "ANALYSIS SUCCESSFUL".
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    write_project(
        out_dir,
        1,
        issues={
            "runs": [
                {
                    "results": [
                        {"ruleId": "CS0169", "level": "warning", "locations": []},
                        {
                            "ruleId": "CS1002",
                            "level": "error",
                            "message": "; expected",
                            "locations": [{"resultFile": {"uri": "file:///w/Broken/Broken.cs"}}],
                        },
                    ]
                }
            ]
        },
    )
    write_project(out_dir, 2, project_info_xml("Tiny.Core.Tests", TESTS_GUID))
    write_engine_input(out_dir, [CORE_GUID, TESTS_GUID])

    problems = find_skipped_projects(out_dir, "INFO: ANALYSIS SUCCESSFUL")
    assert len(problems) == 1
    assert "out/1" in problems[0]
    assert "did not finish building" in problems[0]
    assert "CS1002" in problems[0] and "/w/Broken/Broken.cs" in problems[0]


def test_partial_build_without_issues_json_still_skipped(out_dir):
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    write_project(out_dir, 1)
    write_engine_input(out_dir, [CORE_GUID])
    problems = find_skipped_projects(out_dir, "")
    assert len(problems) == 1 and "out/1" in problems[0]


def test_partial_build_with_only_warnings_in_issues_json_has_no_hint(out_dir):
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    write_project(out_dir, 1, issues={"runs": [{"results": [{"ruleId": "CS0169", "level": "warning"}]}]})
    write_engine_input(out_dir, [CORE_GUID])
    problems = find_skipped_projects(out_dir, "")
    assert problems == ["out/1: a project started building but did not finish building (no ProjectInfo.xml)"]


def test_partial_build_with_unreadable_issues_json_still_skipped(out_dir):
    project_dir = write_project(out_dir, 1)
    (project_dir / "Issues.json").write_text("{not json")
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    problems = find_skipped_projects(out_dir, "")
    assert len(problems) == 1 and "out/1" in problems[0]


def test_project_missing_from_analyzed_modules_is_skipped(out_dir):
    # Duplicate/invalid ProjectGuid: ProjectInfo.xml exists, but `end`
    # dropped the project from sonar.modules.
    dup = "11111111-2222-3333-4444-555555555555"
    write_project(out_dir, 0, project_info_xml("Tiny.Core", dup))
    write_project(out_dir, 1, project_info_xml("Other", CORE_GUID))
    write_project(out_dir, 2, project_info_xml("Tiny.Core.Tests", dup))
    write_engine_input(out_dir, [CORE_GUID])

    problems = find_skipped_projects(out_dir, "")
    assert len(problems) == 2
    assert all("not analyzed by `end`" in p for p in problems)
    assert any("Tiny.Core (/w/Tiny.Core/Tiny.Core.csproj)" in p for p in problems)


def test_log_lines_are_reported_too(out_dir):
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    line = 'WARNING: Duplicate ProjectGuid: "0". The project will not be analyzed. Project file: x'
    problems = find_skipped_projects(out_dir, line + "\n")
    assert problems == [f"scanner log: {line}"]
    assert problems[0].code == "project-not-analyzed"


def test_sonarqube_exclude_project_passes(out_dir):
    # The reviewer's live repro: one <SonarQubeExclude> project + one
    # normal one. `end` logs the exclude-flag line, ProjectInfo.xml says
    # IsExcluded=true, sonar.modules holds only the normal project.
    write_project(out_dir, 0, project_info_xml("Normal", CORE_GUID))
    write_project(out_dir, 1, project_info_xml("Excluded", TESTS_GUID, excluded=True))
    write_engine_input(out_dir, [CORE_GUID])
    assert find_skipped_projects(out_dir, EXCLUDE_FLAG_LINE + "\n") == []


# --- failure reason codes -------------------------------------------------------


def test_reason_code_project_not_built(out_dir):
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    write_project(out_dir, 1)
    write_engine_input(out_dir, [CORE_GUID])
    assert [p.code for p in find_skipped_projects(out_dir, "")] == ["project-not-built"]


def test_reason_code_project_not_analyzed(out_dir):
    write_project(out_dir, 0, project_info_xml("A", CORE_GUID))
    write_project(out_dir, 1, project_info_xml("B", TESTS_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    assert [p.code for p in find_skipped_projects(out_dir, "")] == ["project-not-analyzed"]


def test_reason_code_malformed_project_info(out_dir):
    write_project(out_dir, 0, "<ProjectInfo>")
    write_project(out_dir, 1, project_info_xml("Tiny.Core", CORE_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    assert [p.code for p in find_skipped_projects(out_dir, "")] == ["project-not-analyzed"]


@pytest.mark.parametrize("setup", ["missing-out", "missing-engine-input", "empty-modules"])
def test_reason_code_no_analysis_output(tmp_path, out_dir, setup):
    target = out_dir
    if setup == "missing-out":
        target = tmp_path / "nope"
    elif setup == "empty-modules":
        write_engine_input(out_dir, [])
    assert [p.code for p in find_skipped_projects(target, "")] == ["no-analysis-output"]


def test_reason_codes_are_sorted_and_unique(out_dir):
    write_project(out_dir, 0, project_info_xml("A", CORE_GUID))
    write_project(out_dir, 1)
    write_project(out_dir, 2)
    write_project(out_dir, 3, project_info_xml("B", TESTS_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    assert reason_codes(find_skipped_projects(out_dir, "")) == "project-not-analyzed,project-not-built"
    assert reason_codes([]) == ""


# --- read_compiled_files ------------------------------------------------------------


def _with_files_to_analyze(tmp_path, out_dir, index, name, guid, files, excluded=False):
    conf = tmp_path / ".sonarqube" / "conf" / str(index)
    conf.mkdir(parents=True)
    listing = conf / "FilesToAnalyze.txt"
    # The real file starts with a UTF-8 BOM.
    listing.write_bytes(b"\xef\xbb\xbf" + "\n".join(str(f) for f in files).encode() + b"\n")
    write_project(out_dir, index, project_info_xml(name, guid, excluded=excluded, files_location=str(listing)))


def test_read_compiled_files_unions_every_project_including_excluded(tmp_path, out_dir):
    a = tmp_path / "A" / "A.cs"
    b = tmp_path / "B" / "B.cs"
    _with_files_to_analyze(tmp_path, out_dir, 0, "A", CORE_GUID, [a])
    _with_files_to_analyze(tmp_path, out_dir, 1, "B", TESTS_GUID, [b], excluded=True)
    assert read_compiled_files(out_dir) == {os.path.realpath(a), os.path.realpath(b)}


def test_read_compiled_files_skips_projects_without_a_listing(tmp_path, out_dir):
    write_project(out_dir, 0, project_info_xml("Empty", CORE_GUID, files_to_analyze=False))
    write_project(out_dir, 1, project_info_xml("Gone", TESTS_GUID, files_location=str(tmp_path / "missing.txt")))
    write_project(out_dir, 2)
    write_project(out_dir, 3, "<ProjectInfo>")
    assert read_compiled_files(out_dir) == set()


def test_read_compiled_files_missing_out_dir(tmp_path):
    assert read_compiled_files(tmp_path / "nope") == set()


def test_project_with_no_files_to_analyze_is_not_a_skip(out_dir):
    # A project with no source files (e.g. a packaging-only .csproj) has no
    # FilesToAnalyze and is legitimately left out of sonar.modules.
    write_project(out_dir, 0, project_info_xml("Empty", "48d6a118-de14-c909-95d0-a9f847b0c7fa", files_to_analyze=False))
    write_project(out_dir, 1, project_info_xml("Tiny.Core", CORE_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    assert find_skipped_projects(out_dir, "") == []


def test_excluded_project_is_not_a_skip(out_dir):
    write_project(out_dir, 0, project_info_xml("Fakes", TESTS_GUID, excluded=True))
    write_project(out_dir, 1, project_info_xml("Tiny.Core", CORE_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    assert find_skipped_projects(out_dir, "") == []


def test_missing_engine_input_fails_closed(out_dir):
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    problems = find_skipped_projects(out_dir, "")
    assert problems == [f"{out_dir}/ScannerEngineInput.json is missing - cannot confirm which projects `end` analyzed"]


def test_no_analyzed_module_fails_closed(out_dir):
    write_engine_input(out_dir, [])
    assert find_skipped_projects(out_dir, "") == ["`end` analyzed no project at all (sonar.modules is empty)"]


def test_missing_out_dir_fails_closed(tmp_path):
    problems = find_skipped_projects(tmp_path / "nope", "")
    assert len(problems) == 1 and "does not exist" in problems[0]


def test_non_numeric_entries_under_out_are_ignored(out_dir):
    (out_dir / ".sonar").mkdir()
    (out_dir / "Processed.Telemetry.S4NET.json").write_text("{}")
    write_project(out_dir, 0, project_info_xml("Tiny.Core", CORE_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    assert find_skipped_projects(out_dir, "") == []


def test_malformed_project_info_is_reported(out_dir):
    write_project(out_dir, 0, "<ProjectInfo>")
    write_project(out_dir, 1, project_info_xml("Tiny.Core", CORE_GUID))
    write_engine_input(out_dir, [CORE_GUID])
    problems = find_skipped_projects(out_dir, "")
    assert len(problems) == 1 and "out/0" in problems[0]
