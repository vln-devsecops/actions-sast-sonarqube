"""Specs for scripts/run_scan.sh, run for real under bash with `docker`,
`curl`, `dotnet` and `dotnet-sonarscanner` replaced by fakes on PATH that
record how they were invoked. Covers mode selection and input validation,
the `cli` invocation staying byte-for-byte what it was before `dotnet`
mode existed (regression), and the `dotnet` begin/build/end sequence and
its fail-closed paths. Live behaviour against a real SonarQube is covered
by ci.yml's dotnet jobs and docs/test-procedure.md."""
import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_SCAN = REPO_ROOT / "scripts" / "run_scan.sh"
SCANNER_IMAGE = "sonarsource/sonar-scanner-cli:12.0.0.0000_7.1"
TOKEN = "squ_faketoken0123456789"
HOST = "http://localhost:9000"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="run_scan.sh specs need bash and jq",
)

FAKE_DOCKER = r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "compose" ]]; then
  echo "sonarqube:26.8.0.126808-community"
  echo "__IMAGE__"
  exit 0
fi
if [[ "$1" == "run" ]]; then
  printf '%s\0' "$@" > "$FAKE_LOG_DIR/docker-run.args"
  prev=""
  for arg in "$@"; do
    if [[ "$prev" == "-v" && "$arg" == *":/usr/src" ]]; then
      src="${arg%:/usr/src}"
      mkdir -p "$src/.scannerwork"
      echo "ceTaskId=CLI-TASK" > "$src/.scannerwork/report-task.txt"
    fi
    prev="$arg"
  done
  if [[ -n "${FAKE_DOCKER_RUN_FAIL:-}" ]]; then
    echo "ERROR Error during SonarScanner Engine execution"
    exit 2
  fi
  echo "INFO  EXECUTION SUCCESS"
  exit 0
fi
echo "unexpected docker call: $*" >&2
exit 99
""".replace("__IMAGE__", SCANNER_IMAGE)

FAKE_CURL = r"""#!/usr/bin/env bash
echo "$*" >> "$FAKE_LOG_DIR/curl.calls"
echo '{"task":{"status":"SUCCESS"}}'
"""

# `dotnet tool install <id> --version <v> --tool-path <dir>` drops the fake
# scanner below into <dir>, the way the real one would.
FAKE_DOTNET = r"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\0' "$@" > "$FAKE_LOG_DIR/dotnet-tool.args"
if [[ "$1 $2" != "tool install" ]]; then
  echo "unexpected dotnet call: $*" >&2
  exit 99
fi
tool_path=""
prev=""
for arg in "$@"; do
  [[ "$prev" == "--tool-path" ]] && tool_path="$arg"
  prev="$arg"
done
mkdir -p "$tool_path"
cp "$FAKE_SCANNER_SRC" "$tool_path/dotnet-sonarscanner"
chmod +x "$tool_path/dotnet-sonarscanner"
"""

# Mimics the files the real dotnet-sonarscanner 12.1.0 leaves behind (see
# tests/test_check_dotnet_projects.py) according to FAKE_END_MODE:
#   ok        one project, built and analyzed
#   partial   ok + an out/<n>/ dir with no ProjectInfo.xml (build failed)
#   duplicate ok + a project `end` dropped (WARNING ... will not be analyzed)
#   fail      `end` exits 1
#   noreport  `end` exits 0 but writes no report-task.txt
FAKE_SCANNER = r"""#!/usr/bin/env bash
set -euo pipefail
step="$1"
printf '%s\0' "$@" > "$FAKE_LOG_DIR/scanner-$step.args"
pwd > "$FAKE_LOG_DIR/scanner-$step.cwd"
if [[ "$step" == "begin" ]]; then
  [[ "${FAKE_BEGIN_MODE:-ok}" == "fail" ]] && { echo "begin failed" >&2; exit 1; }
  mkdir -p .sonarqube/conf
  echo "Pre-processing succeeded."
  exit 0
fi
mode="${FAKE_END_MODE:-ok}"
[[ "$mode" == "fail" ]] && { echo "Post-processing failed. Exit code: 1"; exit 1; }
if [[ "$mode" == "noanalyzable" ]]; then
  echo "No analyzable projects were found. SonarQube analysis will not be performed."
  echo "Post-processing failed. Exit code: 1"
  exit 1
fi
out=.sonarqube/out
mkdir -p "$out/0" "$out/.sonar" .sonarqube/conf/0
# The real listing starts with a UTF-8 BOM; Program.cs is the only file the
# "build" compiled.
printf '\xef\xbb\xbf%s\n' "$PWD/Program.cs" > .sonarqube/conf/0/FilesToAnalyze.txt
cat > "$out/0/ProjectInfo.xml" <<XML
<?xml version="1.0" encoding="utf-8"?>
<ProjectInfo xmlns="http://www.sonarsource.com/msbuild/integration/2015/1">
  <ProjectName>Tiny.Core</ProjectName>
  <ProjectGuid>742e96f3-4aee-28a3-3120-ce8630155aa7</ProjectGuid>
  <FullPath>/w/Tiny.Core/Tiny.Core.csproj</FullPath>
  <IsExcluded>false</IsExcluded>
  <AnalysisResultFiles><AnalysisResultFile Id="FilesToAnalyze" Location="$PWD/.sonarqube/conf/0/FilesToAnalyze.txt" /></AnalysisResultFiles>
</ProjectInfo>
XML
if [[ "$mode" == "perfile" ]]; then
  # Program.cs excluded per file (<Compile Update><SonarQubeExclude>):
  # gone from FilesToAnalyze, still in the analyzer's file-metadata.pb.
  sed -i 's#<AnalysisResultFiles>.*</AnalysisResultFiles>#<AnalysisResultFiles />#' "$out/0/ProjectInfo.xml"
  mkdir -p "$out/0/output-cs"
  python3 - "$PWD/Program.cs" "$out/0/output-cs/file-metadata.pb" <<'PY'
import sys
def varint(n):
    out = bytearray()
    while True:
        out.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(out)
path = sys.argv[1].encode()
msg = b"\x0a" + varint(len(path)) + path + b"\x1a\x05utf-8"
open(sys.argv[2], "wb").write(varint(len(msg)) + msg)
PY
fi
if [[ "$mode" == "excluded" ]]; then
  mkdir -p "$out/1"
  cat > "$out/1/ProjectInfo.xml" <<'XML'
<?xml version="1.0" encoding="utf-8"?>
<ProjectInfo xmlns="http://www.sonarsource.com/msbuild/integration/2015/1">
  <ProjectName>Excluded</ProjectName>
  <ProjectGuid>467f10aa-28ab-8c29-d665-51bb75b75902</ProjectGuid>
  <FullPath>/w/Excluded/Excluded.csproj</FullPath>
  <IsExcluded>true</IsExcluded>
</ProjectInfo>
XML
  echo "21:03:51.291  The exclude flag has been set so the project will not be analyzed. Project file: /w/Excluded/Excluded.csproj"
fi
echo '{"scannerProperties":[{"key":"sonar.modules","value":"742E96F3-4AEE-28A3-3120-CE8630155AA7"}]}' > "$out/ScannerEngineInput.json"
if [[ "$mode" == "badjson" ]]; then
  # Makes check_dotnet_projects.py crash (json.load) before it decides.
  echo '{not json' > "$out/ScannerEngineInput.json"
fi
if [[ "$mode" == "partial" ]]; then
  mkdir -p "$out/1"
fi
if [[ "$mode" == "duplicate" ]]; then
  echo 'WARNING: Duplicate ProjectGuid: "0". The project will not be analyzed. Project file: "/w/B/B.csproj"'
fi
if [[ "$mode" != "noreport" ]]; then
  echo "ceTaskId=DOTNET-TASK" > "$out/.sonar/report-task.txt"
fi
echo "ANALYSIS SUCCESSFUL"
"""


def _write_exe(path, text):
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def harness(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_exe(bin_dir / "docker", FAKE_DOCKER)
    _write_exe(bin_dir / "curl", FAKE_CURL)
    _write_exe(bin_dir / "dotnet", FAKE_DOTNET)
    fake_scanner = tmp_path / "fake-dotnet-sonarscanner"
    _write_exe(fake_scanner, FAKE_SCANNER)

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    runner_temp = tmp_path / "runner_temp"
    runner_temp.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    (project / "Program.cs").write_text("class P {}\n")
    github_output = tmp_path / "github_output"
    github_output.write_text("")

    # The fakes must shadow the real binaries - on a host whose temp dir is
    # mounted noexec they silently wouldn't, and the "fake" scan would run
    # the real docker. Point TMPDIR (or pytest --basetemp) at an exec-able
    # directory there.
    probe = subprocess.run(
        ["bash", "-c", "command -v docker; docker compose config --images"],
        env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
    )
    if SCANNER_IMAGE not in probe.stdout:
        pytest.fail(f"fake docker in {bin_dir} is not executable (noexec temp dir?): {probe.stdout}{probe.stderr}")

    class Harness:
        pass

    h = Harness()
    h.tmp_path = tmp_path
    h.log_dir = log_dir
    h.project = project
    h.runner_temp = runner_temp
    h.github_output = github_output
    h.env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FAKE_LOG_DIR": str(log_dir),
        "FAKE_SCANNER_SRC": str(fake_scanner),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(github_output),
        "SONAR_HOST_URL": HOST,
        "SONAR_TOKEN": TOKEN,
        "PROJECT_KEY": "my-key",
        "PROJECT_BASE_DIR": str(project),
        "COMPOSE_FILE": str(REPO_ROOT / "docker-compose.ephemeral.yml"),
        "SAST_CONFIG_FILE": str(project / ".sastrc"),
        "SAST_IGNORE_FILE": str(project / ".sastignore"),
    }

    def run(**overrides):
        env = dict(h.env)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return subprocess.run(["bash", str(RUN_SCAN)], env=env, capture_output=True, text=True, timeout=60)

    def args(name):
        path = log_dir / name
        if not path.exists():
            return None
        return path.read_bytes().decode().split("\0")[:-1]

    h.run = run
    h.args = args
    return h


def _normalize_cache_mount(args):
    return [re.sub(r"sonar-scanner-cache\.[A-Za-z0-9]+", "sonar-scanner-cache.XXXXXX", a) for a in args]


def _expected_cli_args(h, extra=()):
    return [
        "run",
        "--rm",
        "--network",
        "host",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "-e",
        f"SONAR_HOST_URL={HOST}",
        "-e",
        f"SONAR_TOKEN={TOKEN}",
        "-e",
        "HOME=/tmp",
        "-e",
        "SONAR_USER_HOME=/tmp/sonar-cache",
        "-v",
        f"{h.project}:/usr/src",
        "-v",
        f"{h.runner_temp}/sonar-scanner-cache.XXXXXX:/tmp/sonar-cache",
        "-w",
        "/usr/src",
        SCANNER_IMAGE,
        "-Dsonar.projectKey=my-key",
        "-Dsonar.projectBaseDir=/usr/src",
        "-Dsonar.working.directory=/usr/src/.scannerwork",
        *extra,
    ]


# --- cli mode: regression ------------------------------------------------------


def test_cli_default_invocation_is_unchanged(harness):
    # No new inputs set at all (SCANNER_MODE/BUILD_COMMAND/... absent):
    # exactly the docker invocation this script made before dotnet mode.
    result = harness.run()
    assert result.returncode == 0, result.stderr
    assert _normalize_cache_mount(harness.args("docker-run.args")) == _expected_cli_args(harness)
    assert harness.args("dotnet-tool.args") is None
    assert "Compute engine task succeeded." in result.stdout


def test_cli_explicit_mode_matches_default(harness):
    result = harness.run(SCANNER_MODE="cli", BUILD_COMMAND="", IMPORT_ROSLYN_ISSUES="false")
    assert result.returncode == 0, result.stderr
    assert _normalize_cache_mount(harness.args("docker-run.args")) == _expected_cli_args(harness)


def test_cli_invocation_with_sastrc_and_scm_disabled_is_unchanged(harness):
    (harness.project / ".sastrc").write_text('tests:\n  paths: ["tests"]\n')
    (harness.project / ".sastignore").write_text("vendor/\n")
    result = harness.run(SCM_DISABLED="true")
    assert result.returncode == 0, result.stderr
    assert _normalize_cache_mount(harness.args("docker-run.args")) == _expected_cli_args(
        harness,
        extra=[
            "-Dsonar.scm.disabled=true",
            "-Dsonar.exclusions=**/vendor/**",
            "-Dsonar.sources=.",
            "-Dsonar.tests=.",
            "-Dsonar.test.inclusions=tests/**",
        ],
    )


def test_cli_rejects_a_build_command(harness):
    # A build-command in cli mode would be silently ignored - and the
    # caller would believe their C# was built and analyzed.
    result = harness.run(SCANNER_MODE="cli", BUILD_COMMAND="dotnet build")
    assert result.returncode != 0
    assert "build-command is only used with scanner-mode: dotnet" in result.stderr
    assert harness.args("docker-run.args") is None


def test_unknown_scanner_mode_fails(harness):
    result = harness.run(SCANNER_MODE="msbuild")
    assert result.returncode != 0
    assert "scanner-mode must be 'cli' or 'dotnet'" in result.stderr
    assert harness.args("docker-run.args") is None


# --- dotnet mode -----------------------------------------------------------------


def _dotnet(harness, **overrides):
    env = {"SCANNER_MODE": "dotnet", "BUILD_COMMAND": f"echo built > {harness.log_dir}/build.ran"}
    env.update(overrides)
    return harness.run(**env)


def _pinned_scanner_version():
    manifest = json.loads((REPO_ROOT / ".config" / "dotnet-tools.json").read_text())
    return manifest["tools"]["dotnet-sonarscanner"]["version"]


def test_pinned_scanner_version_is_12_1_0():
    assert _pinned_scanner_version() == "12.1.0"


def test_dotnet_without_build_command_fails_before_scanning(harness):
    result = harness.run(SCANNER_MODE="dotnet", BUILD_COMMAND="")
    assert result.returncode != 0
    assert "build-command is required when scanner-mode is dotnet" in result.stderr
    assert harness.args("dotnet-tool.args") is None


def test_dotnet_happy_path_runs_begin_build_end_and_polls(harness):
    result = _dotnet(harness)
    assert result.returncode == 0, result.stderr + result.stdout

    tool_args = harness.args("dotnet-tool.args")
    assert tool_args[:5] == ["tool", "install", "dotnet-sonarscanner", "--version", _pinned_scanner_version()]
    tool_path = tool_args[tool_args.index("--tool-path") + 1]
    assert tool_path.startswith(f"{harness.runner_temp}/")

    begin = harness.args("scanner-begin.args")
    user_home = [a for a in begin if a.startswith("/d:sonar.userHome=")]
    assert len(user_home) == 1 and user_home[0].startswith(f"/d:sonar.userHome={harness.runner_temp}/")
    assert begin == [
        "begin",
        "/k:my-key",
        f"/d:sonar.host.url={HOST}",
        f"/d:sonar.token={TOKEN}",
        user_home[0],
        f"/d:sonar.projectBaseDir={harness.project}",
        "/d:sonar.cs.roslyn.ignoreIssues=true",
    ]
    assert harness.args("scanner-end.args") == ["end", f"/d:sonar.token={TOKEN}"]
    assert (harness.log_dir / "build.ran").exists()

    # begin, build and end all from project-base-dir.
    assert (harness.log_dir / "scanner-begin.cwd").read_text().strip() == str(harness.project)
    assert (harness.log_dir / "scanner-end.cwd").read_text().strip() == str(harness.project)
    assert "id=DOTNET-TASK" in (harness.log_dir / "curl.calls").read_text()
    assert harness.args("docker-run.args") is None
    assert "Compute engine task succeeded." in result.stdout


def test_dotnet_never_echoes_the_token(harness):
    result = _dotnet(harness)
    assert result.returncode == 0
    assert TOKEN not in result.stdout
    assert TOKEN not in result.stderr


def test_dotnet_build_runs_in_project_base_dir_without_the_sonar_token(harness):
    result = _dotnet(harness, BUILD_COMMAND=f"pwd > {harness.log_dir}/build.cwd; env > {harness.log_dir}/build.env")
    assert result.returncode == 0, result.stderr
    assert (harness.log_dir / "build.cwd").read_text().strip() == str(harness.project)
    build_env = (harness.log_dir / "build.env").read_text()
    assert TOKEN not in build_env
    assert "SONAR_TOKEN=" not in build_env


def test_dotnet_build_command_inherits_nuget_auth_token_when_set(harness):
    """Unlike SONAR_TOKEN (stripped above), NUGET_AUTH_TOKEN is NOT removed
    from build-command's environment - action.yml scopes it to this one
    step so it never reaches the fetch/report steps after it, but within
    this step it reaches build-command by plain inheritance; nothing in
    run_scan.sh needs to special-case it."""
    result = _dotnet(
        harness,
        NUGET_AUTH_TOKEN="nuget-secret-token",
        BUILD_COMMAND=f"env > {harness.log_dir}/build.env",
    )
    assert result.returncode == 0, result.stderr
    build_env = (harness.log_dir / "build.env").read_text()
    assert "NUGET_AUTH_TOKEN=nuget-secret-token" in build_env


def test_dotnet_build_command_is_not_shell_interpolated_by_the_script(harness):
    # The command is handed to `bash -c` as-is: quoting and $-expansion are
    # the build shell's, evaluated once, inside project-base-dir.
    marker = harness.log_dir / "quoted"
    result = _dotnet(harness, BUILD_COMMAND=f"printf '%s' \"$PWD\" '$HOME' > {marker}")
    assert result.returncode == 0, result.stderr
    assert marker.read_text() == f"{harness.project}$HOME"


def test_dotnet_import_roslyn_issues_true_drops_ignore_flag(harness):
    result = _dotnet(harness, IMPORT_ROSLYN_ISSUES="true")
    assert result.returncode == 0, result.stderr
    assert not any(a.startswith("/d:sonar.cs.roslyn.") for a in harness.args("scanner-begin.args"))


@pytest.mark.parametrize("value", ["false", "", "TRUE", "yes"])
def test_dotnet_import_roslyn_issues_anything_but_true_ignores_them(harness, value):
    result = _dotnet(harness, IMPORT_ROSLYN_ISSUES=value)
    assert result.returncode == 0, result.stderr
    assert "/d:sonar.cs.roslyn.ignoreIssues=true" in harness.args("scanner-begin.args")


def test_dotnet_passes_sastrc_and_scm_disabled_as_slash_d(harness):
    (harness.project / ".sastrc").write_text('exclusions:\n  duplication_paths: ["**/Migrations/**"]\n')
    (harness.project / ".sastignore").write_text("Generated/\n")
    result = _dotnet(harness, SCM_DISABLED="true")
    assert result.returncode == 0, result.stderr
    assert harness.args("scanner-begin.args")[-3:] == [
        "/d:sonar.scm.disabled=true",
        "/d:sonar.exclusions=**/Generated/**",
        "/d:sonar.cpd.exclusions=**/Migrations/**",
    ]


def test_dotnet_rejects_sastrc_tests_paths_before_begin(harness):
    (harness.project / ".sastrc").write_text('tests:\n  paths: ["Tests/**"]\n')
    result = _dotnet(harness)
    assert result.returncode != 0
    assert "tests.paths" in result.stderr
    assert harness.args("scanner-begin.args") is None


def test_dotnet_cleans_stale_sonarqube_dir_before_begin(harness):
    stale = harness.project / ".sonarqube" / "out" / "7"
    stale.mkdir(parents=True)
    result = _dotnet(harness)
    assert result.returncode == 0, result.stderr
    assert not stale.exists()


def test_dotnet_failing_build_fails_without_running_end(harness):
    result = _dotnet(harness, BUILD_COMMAND="exit 3")
    assert result.returncode != 0
    assert "build-command exited with status 3" in result.stderr
    assert harness.args("scanner-end.args") is None
    assert not (harness.log_dir / "curl.calls").exists()


def test_dotnet_failing_begin_fails(harness):
    result = _dotnet(harness, FAKE_BEGIN_MODE="fail")
    assert result.returncode != 0
    assert not (harness.log_dir / "build.ran").exists()


def test_dotnet_failing_end_fails(harness):
    result = _dotnet(harness, FAKE_END_MODE="fail")
    assert result.returncode != 0
    assert not (harness.log_dir / "curl.calls").exists()


def test_dotnet_partial_build_fails_even_though_end_succeeded(harness):
    result = _dotnet(harness, FAKE_END_MODE="partial")
    assert "ANALYSIS SUCCESSFUL" in result.stdout
    assert result.returncode != 0
    assert "did not finish building" in result.stderr
    assert not (harness.log_dir / "curl.calls").exists()


def test_dotnet_skipped_project_in_end_log_fails(harness):
    result = _dotnet(harness, FAKE_END_MODE="duplicate")
    assert result.returncode != 0
    assert "will not be analyzed" in result.stderr


def test_dotnet_missing_report_task_fails_with_its_path(harness):
    result = _dotnet(harness, FAKE_END_MODE="noreport")
    assert result.returncode != 0
    assert f"{harness.project}/.sonarqube/out/.sonar/report-task.txt" in result.stderr


def test_dotnet_does_not_report_csharp_as_unanalyzed(harness):
    result = _dotnet(harness)
    assert result.returncode == 0, result.stderr
    assert "unanalyzed-languages=\n" in harness.github_output.read_text()


def test_dotnet_still_reports_extension_detected_languages(harness):
    (harness.project / "native.cpp").write_text("int main() {}\n")
    result = _dotnet(harness)
    assert result.returncode == 0, result.stderr
    assert "unanalyzed-languages=C++\n" in harness.github_output.read_text()


# --- failure-reason output --------------------------------------------------------
# Each failure path names its cause in a `failure-reason` step output, so
# ci.yml's bad cases can assert *why* the action failed, not just that it
# did (a typo'd fixture path or a broken setup would fail too).


def _output(harness, name):
    values = [
        line.split("=", 1)[1]
        for line in harness.github_output.read_text().splitlines()
        if line.startswith(f"{name}=")
    ]
    return values[-1] if values else None


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"SCANNER_MODE": "msbuild"}, "invalid-scanner-mode"),
        ({"SCANNER_MODE": "cli", "BUILD_COMMAND": "dotnet build"}, "build-command-not-allowed"),
        ({"SCANNER_MODE": "dotnet", "BUILD_COMMAND": ""}, "build-command-required"),
        ({"PROJECT_BASE_DIR": "/nonexistent/project"}, "project-base-dir-missing"),
    ],
)
def test_failure_reason_for_input_validation(harness, overrides, reason):
    result = harness.run(**overrides)
    assert result.returncode != 0
    assert _output(harness, "failure-reason") == reason


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"BUILD_COMMAND": "exit 3"}, "build-command-failed"),
        ({"FAKE_BEGIN_MODE": "fail"}, "begin-failed"),
        ({"FAKE_END_MODE": "fail"}, "end-failed"),
        ({"FAKE_END_MODE": "noanalyzable"}, "no-analyzable-projects"),
        ({"FAKE_END_MODE": "partial"}, "project-not-built"),
        ({"FAKE_END_MODE": "duplicate"}, "project-not-analyzed"),
        ({"FAKE_END_MODE": "noreport"}, "no-report-task"),
    ],
)
def test_failure_reason_for_dotnet_failures(harness, overrides, reason):
    result = _dotnet(harness, **overrides)
    assert result.returncode != 0
    assert _output(harness, "failure-reason") == reason


def test_failure_reason_for_rejected_sastrc(harness):
    (harness.project / ".sastrc").write_text('tests:\n  paths: ["Tests/**"]\n')
    result = _dotnet(harness)
    assert result.returncode != 0
    assert _output(harness, "failure-reason") == "invalid-sast-config"


def test_no_failure_reason_on_success(harness):
    assert _dotnet(harness).returncode == 0
    assert _output(harness, "failure-reason") is None
    assert harness.run().returncode == 0
    assert _output(harness, "failure-reason") is None


def test_failure_reason_also_goes_to_the_step_summary(harness):
    summary = harness.tmp_path / "summary.md"
    summary.write_text("")
    result = _dotnet(harness, BUILD_COMMAND="exit 3", GITHUB_STEP_SUMMARY=str(summary))
    assert result.returncode != 0
    assert "build-command-failed" in summary.read_text()


# --- dotnet mode: SonarQubeExclude and uncompiled C# ---------------------------------


def test_dotnet_sonarqube_exclude_project_does_not_fail(harness):
    result = _dotnet(harness, FAKE_END_MODE="excluded")
    assert result.returncode == 0, result.stderr
    assert "Compute engine task succeeded." in result.stdout


def test_dotnet_per_file_sonarqube_exclude_is_not_reported_as_unanalyzed(harness):
    result = _dotnet(harness, FAKE_END_MODE="perfile")
    assert result.returncode == 0, result.stderr
    assert "unanalyzed-languages=\n" in harness.github_output.read_text()


def test_dotnet_check_crash_gets_its_own_failure_reason(harness):
    # check_dotnet_projects.py dying before it writes a reason must not be
    # mistaken for a skipped project.
    result = _dotnet(harness, FAKE_END_MODE="badjson")
    assert result.returncode != 0
    assert _output(harness, "failure-reason") == "check-failed"


def test_cli_scanner_failure_has_a_failure_reason_and_unchanged_args(harness):
    result = harness.run(FAKE_DOCKER_RUN_FAIL="1")
    assert result.returncode != 0
    assert _output(harness, "failure-reason") == "scanner-failed"
    assert _normalize_cache_mount(harness.args("docker-run.args")) == _expected_cli_args(harness)


def test_dotnet_reports_csharp_the_build_did_not_compile(harness):
    (harness.project / "Unbuilt").mkdir()
    (harness.project / "Unbuilt" / "Unbuilt.cs").write_text("class U {}\n")
    result = _dotnet(harness)
    # Reported, not failed - fail-on-unanalyzed-languages decides that.
    assert result.returncode == 0, result.stderr
    assert "unanalyzed-languages=C#\n" in harness.github_output.read_text()
