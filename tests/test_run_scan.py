"""Behavioural tests for scripts/run_scan.sh's project-base-dir handling.

`docker` is replaced by a stub on PATH that records its arguments, so these
tests never start a container: they only check what run_scan.sh would mount
and whether it gets that far at all.
"""
import os
import re
import subprocess
from pathlib import Path

import pytest

RUN_SCAN = Path(__file__).resolve().parent.parent / "scripts" / "run_scan.sh"

FAKE_DOCKER = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$FAKE_DOCKER_LOG"
if [[ "$1" == "compose" ]]; then
  echo "sonarsource/sonar-scanner-cli:test"
  exit 0
fi
# Report a failed scan, so run_scan.sh stops right after `docker run`
# without polling a server.
exit 1
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner_temp = tmp_path / "runner-temp"
    runner_temp.mkdir()
    log = tmp_path / "docker.log"

    variables = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "SONAR_HOST_URL": "http://localhost:9000",
        "SONAR_TOKEN": "test-token",
        "PROJECT_KEY": "test",
        "COMPOSE_FILE": str(tmp_path / "compose.yml"),
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_WORKSPACE": str(workspace),
        "FAKE_DOCKER_LOG": str(log),
    }
    return {"vars": variables, "workspace": workspace, "log": log, "tmp": tmp_path}


def _run(env, project_base_dir, cwd):
    variables = dict(env["vars"])
    variables["PROJECT_BASE_DIR"] = project_base_dir
    variables["SAST_CONFIG_FILE"] = f"{project_base_dir}/.sastrc"
    variables["SAST_IGNORE_FILE"] = f"{project_base_dir}/.sastignore"
    return subprocess.run(
        ["bash", str(RUN_SCAN)], env=variables, cwd=cwd, capture_output=True, text=True
    )


def _docker_run_calls(env):
    log = env["log"]
    calls = log.read_text().splitlines() if log.exists() else []
    return [c for c in calls if c.startswith("run ")]


def _docker_calls(env):
    log = env["log"]
    return log.read_text().splitlines() if log.exists() else []


def test_scans_regular_directory_inside_workspace(env):
    project = env["workspace"] / "app"
    project.mkdir()
    proc = _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert f"-v {project.resolve()}:/usr/src " in run_calls[0]


def test_scans_workspace_root(env):
    proc = _run(env, ".", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert f"-v {env['workspace'].resolve()}:/usr/src " in run_calls[0]


def test_scanner_invocation_is_unchanged_for_a_normal_project(env):
    project = env["workspace"] / "app"
    project.mkdir()
    (project / ".sastignore").write_text("*.log\n")
    _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1
    expected = (
        r"run --rm --network host --user \d+:\d+ "
        r"-e SONAR_HOST_URL=http://localhost:9000 -e SONAR_TOKEN=test-token "
        r"-e HOME=/tmp -e SONAR_USER_HOME=/tmp/sonar-cache "
        + re.escape(f"-v {project.resolve()}:/usr/src ")
        + re.escape(f"-v {env['vars']['RUNNER_TEMP']}/sonar-scanner-cache.")
        + r"\w+:/tmp/sonar-cache -w /usr/src sonarsource/sonar-scanner-cli:test "
        r"-Dsonar\.projectKey=test -Dsonar\.projectBaseDir=/usr/src "
        r"-Dsonar\.working\.directory=/usr/src/\.scannerwork "
        r"-Dsonar\.exclusions=\*\*/\*\.log,\*\*/\*\.log/\*\*"
    )
    assert re.fullmatch(expected, run_calls[0]), run_calls[0]


def test_mounts_resolved_path_for_symlink_staying_inside_workspace(env):
    real = env["workspace"] / "real-app"
    real.mkdir()
    (env["workspace"] / "app").symlink_to(real, target_is_directory=True)
    proc = _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert f"-v {real.resolve()}:/usr/src " in run_calls[0]


def test_refuses_project_base_dir_symlinked_outside_workspace(env):
    outside = env["tmp"] / "outside"
    outside.mkdir()
    (env["workspace"] / "app").symlink_to(outside, target_is_directory=True)
    proc = _run(env, "app", env["workspace"])
    assert proc.returncode != 0
    assert "outside GITHUB_WORKSPACE" in proc.stderr
    assert _docker_calls(env) == []


def test_refuses_project_base_dir_path_outside_workspace(env):
    outside = env["tmp"] / "outside"
    outside.mkdir()
    proc = _run(env, "../outside", env["workspace"])
    assert proc.returncode != 0
    assert "outside GITHUB_WORKSPACE" in proc.stderr
    assert _docker_calls(env) == []


def test_refuses_sibling_directory_sharing_workspace_prefix(env):
    sibling = env["tmp"] / "workspace-other"
    sibling.mkdir()
    proc = _run(env, str(sibling), env["workspace"])
    assert proc.returncode != 0
    assert "outside GITHUB_WORKSPACE" in proc.stderr
    assert _docker_calls(env) == []


def test_without_github_workspace_any_directory_is_scanned(env):
    # Outside GitHub Actions (a manual local run) there is no workspace to
    # confine to; the resolved path is still what gets mounted.
    del env["vars"]["GITHUB_WORKSPACE"]
    elsewhere = env["tmp"] / "elsewhere"
    elsewhere.mkdir()
    proc = _run(env, str(elsewhere), env["tmp"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert f"-v {elsewhere.resolve()}:/usr/src " in run_calls[0]


def test_refuses_symlinked_config_file_before_starting_scanner(env):
    project = env["workspace"] / "app"
    project.mkdir()
    outside = env["tmp"] / "outside.txt"
    outside.write_text("UNIQUE-CONTENT-MARKER-7f3a\n")
    (project / ".sastignore").symlink_to(outside)
    proc = _run(env, "app", env["workspace"])
    assert proc.returncode != 0
    assert "UNIQUE-CONTENT-MARKER-7f3a" not in proc.stdout + proc.stderr
    assert _docker_run_calls(env) == []
