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
  if [[ "$*" == *"--images"* ]]; then
    echo "sonarsource/sonar-scanner-cli:test"
  elif [[ "$*" == *"ps -q sonarqube"* ]]; then
    # FAKE_SONARQUBE_CONTAINER_IDS: whitespace-separated container ids, one
    # per matching container (empty/unset -> exactly one, the common case;
    # set to "" explicitly to simulate none found).
    if [[ -n "${FAKE_SONARQUBE_CONTAINER_IDS+x}" ]]; then
      printf '%s\\n' ${FAKE_SONARQUBE_CONTAINER_IDS}
    else
      echo "fake-sonarqube-container-id"
    fi
  fi
  exit 0
fi
if [[ "$1" == "inspect" ]]; then
  # FAKE_SONAR_NETWORKS: whitespace-separated network names the inspected
  # container is attached to (default: exactly one).
  printf '%s\\n' ${FAKE_SONAR_NETWORKS:-fake-project_sonar-ephemeral}
  exit 0
fi
# Record what the scanner would find at the forced project.settings path,
# while the host cache directory still exists.
for arg in "$@"; do
  if [[ "$arg" == *:/tmp/sonar-cache ]]; then
    settings="${arg%:/tmp/sonar-cache}/empty-sonar-project.properties"
    if [[ -f "$settings" && ! -L "$settings" ]]; then
      printf 'settings-size=%s\\n' "$(wc -c < "$settings")" >> "$FAKE_DOCKER_LOG.settings"
    else
      echo "settings-missing" >> "$FAKE_DOCKER_LOG.settings"
    fi
  fi
done
if [[ "${FAKE_DOCKER_RUN_SUCCEEDS:-false}" == "true" && "$1" == "run" ]]; then
  # Simulate the scanner writing report-task.txt under the writable cache
  # mount (-v host:/tmp/sonar-cache), never under the read-only project
  # mount (-v host:/usr/src:ro).
  for arg in "$@"; do
    if [[ "$arg" == *:/tmp/sonar-cache ]]; then
      cache_dir="${arg%:/tmp/sonar-cache}"
      mkdir -p "${cache_dir}/scannerwork"
      echo "ceTaskId=fake-task-id" > "${cache_dir}/scannerwork/report-task.txt"
    fi
  done
  exit 0
fi
# Report a failed scan, so run_scan.sh stops right after `docker run`
# without polling a server.
exit 1
"""

FAKE_CURL = """#!/usr/bin/env bash
echo '{"task":{"status":"SUCCESS"}}'
"""


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(0o755)
    curl = bin_dir / "curl"
    curl.write_text(FAKE_CURL)
    curl.chmod(0o755)

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


def _run(env, project_base_dir, cwd, extra_env=None):
    variables = dict(env["vars"])
    variables["PROJECT_BASE_DIR"] = project_base_dir
    variables["SAST_CONFIG_FILE"] = f"{project_base_dir}/.sastrc"
    variables["SAST_IGNORE_FILE"] = f"{project_base_dir}/.sastignore"
    if extra_env:
        variables.update(extra_env)
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
    assert f"-v {project.resolve()}:/usr/src:ro " in run_calls[0]


def test_scans_workspace_root(env):
    proc = _run(env, ".", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert f"-v {env['workspace'].resolve()}:/usr/src:ro " in run_calls[0]


def test_scanner_invocation_is_otherwise_unchanged_for_a_normal_project(env):
    """Pins the exact `docker run` invocation. Only three things differ from
    the pre-isolation invocation: the project mount is `:ro`, the container
    joins the ephemeral stack's own compose network (derived, here
    "fake-project_sonar-ephemeral" per FAKE_DOCKER's default) instead of
    `--network host`, and sonar.working.directory moved under the writable
    cache mount instead of the (now read-only) project mount."""
    project = env["workspace"] / "app"
    project.mkdir()
    (project / ".sastignore").write_text("*.log\n")
    _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1
    expected = (
        r"run --rm --network fake-project_sonar-ephemeral --user \d+:\d+ "
        r"-e SONAR_HOST_URL=http://sonarqube:9000 -e SONAR_TOKEN=test-token "
        r"-e HOME=/tmp -e SONAR_USER_HOME=/tmp/sonar-cache "
        + re.escape(f"-v {project.resolve()}:/usr/src:ro ")
        + re.escape(f"-v {env['vars']['RUNNER_TEMP']}/sonar-scanner-cache.")
        + r"\w+:/tmp/sonar-cache -w /usr/src sonarsource/sonar-scanner-cli:test "
        r"-Dsonar\.projectKey=test -Dsonar\.projectBaseDir=/usr/src "
        r"-Dsonar\.working\.directory=/tmp/sonar-cache/scannerwork "
        r"-Dproject\.settings=/tmp/sonar-cache/empty-sonar-project\.properties "
        r"-Dsonar\.exclusions=\*\*/\*\.log,\*\*/\*\.log/\*\*"
    )
    assert re.fullmatch(expected, run_calls[0]), run_calls[0]


def test_project_mount_is_read_only(env):
    project = env["workspace"] / "app"
    project.mkdir()
    _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1
    assert f"-v {project.resolve()}:/usr/src:ro" in run_calls[0]


def test_never_uses_the_host_network(env):
    project = env["workspace"] / "app"
    project.mkdir()
    _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1
    assert "--network host" not in run_calls[0]


def test_scanner_joins_the_sonarqube_service_container_s_own_network_by_default(env):
    project = env["workspace"] / "app"
    project.mkdir()
    proc = _run(
        env, "app", env["workspace"],
        extra_env={"FAKE_SONAR_NETWORKS": "unique-ephemeral-net-7f3a"},
    )
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert "--network unique-ephemeral-net-7f3a " in run_calls[0]
    docker_calls = _docker_calls(env)
    assert any("ps -q sonarqube" in c for c in docker_calls)
    assert any(c.startswith("inspect ") for c in docker_calls)


def test_scanner_reaches_sonarqube_by_compose_service_name_when_host_url_is_default(env):
    project = env["workspace"] / "app"
    project.mkdir()
    _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1
    assert "-e SONAR_HOST_URL=http://sonarqube:9000 " in run_calls[0]


def test_non_default_sonar_host_url_runs_without_the_compose_network(env):
    project = env["workspace"] / "app"
    project.mkdir()
    proc = _run(
        env, "app", env["workspace"],
        extra_env={"SONAR_HOST_URL": "https://sonar.example.internal:9000"},
    )
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert "--network" not in run_calls[0]
    # The custom URL is passed through unchanged for the scanner to use
    # in-container - it is the caller's responsibility to make sure it is
    # reachable from a container on the default bridge network.
    assert "-e SONAR_HOST_URL=https://sonar.example.internal:9000 " in run_calls[0]


def test_non_default_sonar_host_url_does_not_query_the_compose_network(env):
    project = env["workspace"] / "app"
    project.mkdir()
    _run(
        env, "app", env["workspace"],
        extra_env={"SONAR_HOST_URL": "https://sonar.example.internal:9000"},
    )
    docker_calls = _docker_calls(env)
    assert not any("ps -q sonarqube" in c for c in docker_calls)
    assert not any(c.startswith("inspect ") for c in docker_calls)


def test_fails_clearly_when_the_sonarqube_service_container_is_not_found(env):
    project = env["workspace"] / "app"
    project.mkdir()
    proc = _run(
        env, "app", env["workspace"],
        extra_env={"FAKE_SONARQUBE_CONTAINER_IDS": ""},
    )
    assert proc.returncode != 0
    assert "sonarqube" in proc.stderr.lower()
    assert _docker_run_calls(env) == []


def test_fails_clearly_when_the_sonarqube_service_container_has_multiple_networks(env):
    project = env["workspace"] / "app"
    project.mkdir()
    proc = _run(
        env, "app", env["workspace"],
        extra_env={"FAKE_SONAR_NETWORKS": "net-one net-two"},
    )
    assert proc.returncode != 0
    assert "network" in proc.stderr.lower()
    assert _docker_run_calls(env) == []


def test_report_task_is_read_from_the_cache_mount_not_the_project_dir(env):
    project = env["workspace"] / "app"
    project.mkdir()
    proc = _run(
        env, "app", env["workspace"],
        extra_env={"FAKE_DOCKER_RUN_SUCCEEDS": "true"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "Waiting for compute engine task fake-task-id" in proc.stdout
    assert not (project / ".scannerwork").exists()


def test_scanner_is_pointed_at_an_empty_settings_file_outside_the_project(env):
    project = env["workspace"] / "app"
    project.mkdir()
    (project / "sonar-project.properties").write_text("sonar.sources=src\n")
    proc = _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert "-Dproject.settings=/tmp/sonar-cache/empty-sonar-project.properties" in run_calls[0]
    settings_log = Path(str(env["log"]) + ".settings")
    assert settings_log.read_text().split() == ["settings-size=0"]
    # The consumer's own file is untouched.
    assert (project / "sonar-project.properties").read_text() == "sonar.sources=src\n"


def test_project_settings_is_forced_even_without_a_project_properties_file(env):
    project = env["workspace"] / "app"
    project.mkdir()
    _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1
    assert "-Dproject.settings=/tmp/sonar-cache/empty-sonar-project.properties" in run_calls[0]


def test_passes_supported_project_properties_and_warns_about_the_rest(env):
    project = env["workspace"] / "app"
    project.mkdir()
    (project / "sonar-project.properties").write_text(
        "sonar.exclusions=vendor/**\n"
        "sonar.host.url=http://UNIQUE-VALUE-MARKER-41ab:9111\n"
        "sonar.scanner.proxyHost=UNIQUE-VALUE-MARKER-41ab\n"
    )
    proc = _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert run_calls[0].endswith(" -Dsonar.exclusions=vendor/**")
    assert "UNIQUE-VALUE-MARKER-41ab" not in run_calls[0]
    assert "UNIQUE-VALUE-MARKER-41ab" not in proc.stdout + proc.stderr
    warnings = [line for line in proc.stdout.splitlines() if line.startswith("::warning::")]
    assert len(warnings) == 1
    assert "sonar.host.url" in warnings[0] and "sonar.scanner.proxyHost" in warnings[0]


def test_refuses_symlinked_project_properties_before_starting_scanner(env):
    project = env["workspace"] / "app"
    project.mkdir()
    outside = env["tmp"] / "outside.properties"
    outside.write_text("sonar.sources=UNIQUE-CONTENT-MARKER-7f3a\n")
    (project / "sonar-project.properties").symlink_to(outside)
    proc = _run(env, "app", env["workspace"])
    assert proc.returncode != 0
    assert "UNIQUE-CONTENT-MARKER-7f3a" not in proc.stdout + proc.stderr
    assert _docker_run_calls(env) == []


def test_mounts_resolved_path_for_symlink_staying_inside_workspace(env):
    real = env["workspace"] / "real-app"
    real.mkdir()
    (env["workspace"] / "app").symlink_to(real, target_is_directory=True)
    proc = _run(env, "app", env["workspace"])
    run_calls = _docker_run_calls(env)
    assert len(run_calls) == 1, proc.stderr
    assert f"-v {real.resolve()}:/usr/src:ro " in run_calls[0]


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
    assert f"-v {elsewhere.resolve()}:/usr/src:ro " in run_calls[0]


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
