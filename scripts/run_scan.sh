#!/usr/bin/env bash
# Run a SonarQube scan against PROJECT_BASE_DIR and wait for the resulting
# SonarQube background compute-engine task to finish. Two scanner modes:
#
#   cli     (default) sonar-scanner-cli in Docker - unchanged from before
#           `dotnet` mode existed (tests/test_run_scan.py pins the exact
#           `docker run` invocation).
#   dotnet  SonarScanner for .NET on the runner: `begin`, BUILD_COMMAND,
#           `end`, all from PROJECT_BASE_DIR - the only way C# is actually
#           analyzed (the plain CLI indexes .cs files but runs no rules).
#           Fails if the build, `begin` or `end` fail, or if any project the
#           build touched wasn't analyzed (see check_dotnet_projects.py).
#
# Either way it also detects languages the scan didn't actually analyze
# (see detect_unanalyzed_languages.py) and writes an `unanalyzed-languages`
# step output, so a clean scan result is never presented for files that
# were never analyzed in the first place.
#
# Required env: SONAR_HOST_URL, SONAR_TOKEN, PROJECT_KEY, PROJECT_BASE_DIR,
#                COMPOSE_FILE (path to docker-compose.ephemeral.yml, used only
#                to read back the pinned scanner image tag),
#                SAST_CONFIG_FILE, SAST_IGNORE_FILE (paths to the consumer's
#                optional .sastrc / .sastignore noise-reduction files - need
#                not exist)
# Optional env:  SCAN_TIMEOUT_SECONDS (default 600), SCM_DISABLED (default false -
#                set "true" to pass sonar.scm.disabled=true, for a shallow
#                checkout where blame data isn't available),
#                SCANNER_MODE (cli | dotnet, default cli),
#                BUILD_COMMAND (required with SCANNER_MODE=dotnet, rejected
#                otherwise; run with `bash -c` from PROJECT_BASE_DIR, never
#                interpolated into this or any other script),
#                IMPORT_ROSLYN_ISSUES (dotnet only; anything but "true" passes
#                sonar.cs.roslyn.ignoreIssues=true, so only Sonar's own C#
#                rules are reported)
set -euo pipefail

: "${SONAR_HOST_URL:?SONAR_HOST_URL is required}"
: "${SONAR_TOKEN:?SONAR_TOKEN is required}"
: "${PROJECT_KEY:?PROJECT_KEY is required}"
: "${PROJECT_BASE_DIR:?PROJECT_BASE_DIR is required}"
: "${COMPOSE_FILE:?COMPOSE_FILE is required}"
: "${SAST_CONFIG_FILE:?SAST_CONFIG_FILE is required}"
: "${SAST_IGNORE_FILE:?SAST_IGNORE_FILE is required}"

SCAN_TIMEOUT_SECONDS="${SCAN_TIMEOUT_SECONDS:-600}"
POLL_INTERVAL_SECONDS=5
SCANNER_MODE="${SCANNER_MODE:-cli}"
BUILD_COMMAND="${BUILD_COMMAND:-}"
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# fail <reason-code> <message>: print the message, record the reason code
# as the `failure-reason` step output (and in the job summary), exit 1.
# The code is a fixed token chosen by this script, never caller text, so
# callers - ci.yml's bad cases in particular - can assert *why* the scan
# failed, not just that it did.
fail() {
  local reason="$1" message="$2"
  echo "$message" >&2
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
    echo "failure-reason=${reason}" >> "$GITHUB_OUTPUT"
  fi
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then
    echo "### SonarQube scan failed: \`${reason}\`" >> "$GITHUB_STEP_SUMMARY"
  fi
  exit 1
}

case "$SCANNER_MODE" in
  cli)
    # Silently ignoring it would let a caller believe their C# was built
    # and analyzed when the plain CLI never even ran their build.
    if [[ -n "$BUILD_COMMAND" ]]; then
      fail build-command-not-allowed "build-command is only used with scanner-mode: dotnet (scanner-mode is cli)."
    fi
    ;;
  dotnet)
    if [[ -z "$BUILD_COMMAND" ]]; then
      fail build-command-required "build-command is required when scanner-mode is dotnet."
    fi
    ;;
  *)
    fail invalid-scanner-mode "scanner-mode must be 'cli' or 'dotnet', got '${SCANNER_MODE}'."
    ;;
esac

if [[ ! -d "$PROJECT_BASE_DIR" ]]; then
  fail project-base-dir-missing "PROJECT_BASE_DIR '${PROJECT_BASE_DIR}' does not exist or is not a directory."
fi
# Resolve to the physical path (pwd -P), so what gets bind-mounted below is
# exactly the directory that was checked, not a symbolic link to it.
ABS_PROJECT_BASE_DIR="$(cd "$PROJECT_BASE_DIR" && pwd -P)"

# Under GitHub Actions, the directory to scan must be inside the workspace.
# Outside Actions (e.g. a manual local run) there is no workspace to
# confine to, so this check is skipped. It runs before the cli/dotnet
# split, so both scanner modes (and dotnet mode's build-command, which runs
# from this directory) are confined the same way.
if [[ -n "${GITHUB_WORKSPACE:-}" ]]; then
  if ! ABS_WORKSPACE="$(cd "$GITHUB_WORKSPACE" && pwd -P)"; then
    fail workspace-inaccessible "GITHUB_WORKSPACE '${GITHUB_WORKSPACE}' is not an accessible directory."
  fi
  if [[ "$ABS_PROJECT_BASE_DIR" != "$ABS_WORKSPACE" && "$ABS_PROJECT_BASE_DIR" != "$ABS_WORKSPACE"/* ]]; then
    fail project-base-dir-outside-workspace "PROJECT_BASE_DIR '${PROJECT_BASE_DIR}' resolves to '${ABS_PROJECT_BASE_DIR}', outside GITHUB_WORKSPACE '${ABS_WORKSPACE}'."
  fi
fi

# The scanner image runs as a baked-in non-root user (uid 1000) by default,
# and its home directory (/opt/sonar-scanner/.sonar, where it caches
# downloads) is owned by that uid. We override --user to match the host's
# checkout owner so the bind-mounted PROJECT_BASE_DIR is writable - but that
# means uid 1000's own home directory usually *isn't* writable by whichever
# uid we end up running as (e.g. GitHub Actions' non-root `runner` user).
# Give the scanner a dedicated, host-created (so correctly-owned) cache dir
# instead of letting it fall back to the image's built-in one.
#
# Rooted under RUNNER_TEMP, not bare mktemp -d's default of /tmp: this
# directory gets bind-mounted into the container below and the scanner
# extracts (and executes) a bundled JRE into it -- a bind mount inherits its
# source filesystem's mount flags, so on a self-hosted runner whose system
# /tmp is mounted noexec (a real, hardened runner we hit this on), the
# scanner's own JRE launch fails with "Permission denied" no matter what the
# container's own filesystem allows. RUNNER_TEMP is GitHub Actions' own
# per-job scratch directory specifically meant to be writable *and*
# executable; falling back to /tmp keeps this script working unchanged
# outside Actions (e.g. a manual local run), where that risk doesn't apply.
#
# In dotnet mode the same directory is the SonarScanner for .NET's
# sonar.userHome (passed as /d:sonar.userHome on `begin`): that scanner
# provisions its JRE at `begin`, under $HOME/.sonar/cache by default, and
# the SONAR_USER_HOME env var does NOT move it - only /d:sonar.userHome
# does - and a JRE cached on a noexec /tmp breaks `end` the same way.
SCANNER_CACHE_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/sonar-scanner-cache.XXXXXX")"
# EXTRA_ARGS_FILE/SCANNER_LOG_FILE/UNANALYZED_LANGUAGES_FILE are plain text
# (no exec involved, unlike SCANNER_CACHE_DIR's bundled-JRE concern above),
# but rooted under the same RUNNER_TEMP-not-bare-/tmp scratch root anyway:
# a locked-down runner is free to make /tmp unwritable too, not just
# noexec, and keeping every scratch file this script creates under one
# root avoids depending on /tmp being usable at all.
EXTRA_ARGS_FILE="$(mktemp "${RUNNER_TEMP:-/tmp}/sonar-extra-args.XXXXXX")"
SCANNER_LOG_FILE="$(mktemp "${RUNNER_TEMP:-/tmp}/sonar-scanner-log.XXXXXX")"
UNANALYZED_LANGUAGES_FILE="$(mktemp "${RUNNER_TEMP:-/tmp}/sonar-unanalyzed-languages.XXXXXX")"
# dotnet mode only (empty, and so a no-op in the trap, in cli mode): the
# dotnet-sonarscanner tool install (executable, so RUNNER_TEMP for the same
# noexec reason as SCANNER_CACHE_DIR) and the build's captured output.
DOTNET_TOOL_DIR=""
BUILD_LOG_FILE=""
trap 'rm -rf "${SCANNER_CACHE_DIR}" ${DOTNET_TOOL_DIR:+"${DOTNET_TOOL_DIR}"}; rm -f "${EXTRA_ARGS_FILE}" "${SCANNER_LOG_FILE}" "${UNANALYZED_LANGUAGES_FILE}" ${BUILD_LOG_FILE:+"${BUILD_LOG_FILE}"}' EXIT

# The scanner would otherwise load sonar-project.properties from the
# project directory by itself, with every key in it applied. Point its
# project.settings at an empty file outside the project mount instead;
# build_scan_config.py reads the consumer's file as data and passes on only
# the supported keys (see project_properties.py). cli mode only: the
# SonarScanner for .NET never reads sonar-project.properties, so in dotnet
# mode the same allowlisted keys reach `begin` as /d: instead (minus
# sonar.sources/sonar.tests - see filter_dotnet_project_properties()).
EMPTY_PROJECT_SETTINGS_NAME="empty-sonar-project.properties"
: > "${SCANNER_CACHE_DIR}/${EMPTY_PROJECT_SETTINGS_NAME}"

# Translate the consumer's optional .sastrc/.sastignore noise-reduction
# files (neither need exist) into extra scanner properties: -Dkey=value for
# sonar-scanner-cli, /d:key=value for dotnet-sonarscanner `begin` (which
# also rejects .sastrc tests.paths - see build_scan_config.py). This
# always runs - build_scan_config.py itself treats a missing file as
# "contributes nothing" - so a repo with neither file gets the exact same
# scanner invocation as before this feature existed.
build_scan_config_args=(
  --config "$SAST_CONFIG_FILE"
  --ignore-file "$SAST_IGNORE_FILE"
  --project-properties "${PROJECT_BASE_DIR}/sonar-project.properties"
  --project-base-dir "$PROJECT_BASE_DIR"
  --out "$EXTRA_ARGS_FILE"
)
if [[ "${SCM_DISABLED:-false}" == "true" ]]; then
  build_scan_config_args+=(--scm-disabled)
fi
if [[ "$SCANNER_MODE" == "dotnet" ]]; then
  build_scan_config_args+=(--scanner-mode dotnet)
fi
if ! python3 "${SCRIPTS_DIR}/build_scan_config.py" "${build_scan_config_args[@]}"; then
  fail invalid-sast-config "Rejected the SAST config (see the error above)."
fi
readarray -d '' -t EXTRA_SCANNER_ARGS < "$EXTRA_ARGS_FILE"

# --- scanner-mode: dotnet ----------------------------------------------------
# SonarScanner for .NET, on the runner (it has to share the build's process
# tree and directory): begin -> BUILD_COMMAND -> end, every step from
# PROJECT_BASE_DIR, then check_dotnet_projects.py. Leaves report-task.txt at
# .sonarqube/out/.sonar/report-task.txt (same ceTaskId= format as the CLI),
# and the end step's log in SCANNER_LOG_FILE for detect_unanalyzed_languages.py.
scan_with_dotnet() {
  # Single source of truth for the scanner version is .config/dotnet-tools.json,
  # which Dependabot's nuget ecosystem keeps current.
  local manifest="${SCRIPTS_DIR}/../.config/dotnet-tools.json"
  local version
  version="$(jq -r '.tools["dotnet-sonarscanner"].version // empty' "$manifest")"
  if [[ -z "$version" ]]; then
    fail scanner-install-failed "Could not resolve the dotnet-sonarscanner version from ${manifest}."
  fi
  DOTNET_TOOL_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/dotnet-sonarscanner.XXXXXX")"
  BUILD_LOG_FILE="$(mktemp "${RUNNER_TEMP:-/tmp}/dotnet-build-log.XXXXXX")"
  echo "Installing dotnet-sonarscanner ${version}..."
  if ! dotnet tool install dotnet-sonarscanner --version "$version" --tool-path "$DOTNET_TOOL_DIR"; then
    fail scanner-install-failed "Could not install dotnet-sonarscanner ${version} (is the .NET SDK set up?)."
  fi
  local scanner="${DOTNET_TOOL_DIR}/dotnet-sonarscanner"

  # Sonar's own C# rules only by default: a repo's own Roslyn analyzers
  # (StyleCop, CA*) are already enforced by its build, and imported they
  # drown the findings (128 of fhcs-simulator's 159 issues in the spike).
  local roslyn_args=()
  if [[ "${IMPORT_ROSLYN_ISSUES:-false}" != "true" ]]; then
    roslyn_args=(/d:sonar.cs.roslyn.ignoreIssues=true)
  fi

  # Stale ProjectInfo.xml/ScannerEngineInput.json from an earlier run would
  # both confuse `end` and fool check_dotnet_projects.py - same reason as
  # .scannerwork in cli mode.
  rm -rf "${ABS_PROJECT_BASE_DIR}/.sonarqube"

  echo "Scanning ${ABS_PROJECT_BASE_DIR} as project '${PROJECT_KEY}' (SonarScanner for .NET)..."
  # The token goes on the command line of `begin` and `end`: the scanner
  # doesn't honour SONAR_TOKEN from the environment, and needs it on both
  # steps or neither. It is masked in the Actions log and never echoed here.
  # sonar.projectBaseDir: pinned to PROJECT_BASE_DIR so .sastrc/.sastignore
  #   paths resolve against the same root as in cli mode, rather than the
  #   scanner's own guess (since v8: the projects' common root).
  if ! (cd "$ABS_PROJECT_BASE_DIR" && "$scanner" begin \
    "/k:${PROJECT_KEY}" \
    "/d:sonar.host.url=${SONAR_HOST_URL}" \
    "/d:sonar.token=${SONAR_TOKEN}" \
    "/d:sonar.userHome=${SCANNER_CACHE_DIR}" \
    "/d:sonar.projectBaseDir=${ABS_PROJECT_BASE_DIR}" \
    "${roslyn_args[@]}" \
    "${EXTRA_SCANNER_ARGS[@]}"); then
    fail begin-failed "dotnet-sonarscanner begin failed."
  fi

  # BUILD_COMMAND comes from the caller's workflow via env, never
  # interpolated into a script: bash -c parses it exactly once.
  # `env -u SONAR_TOKEN` is hygiene only, NOT a boundary: build code is
  # the code under scan, and it can still read the token from this
  # script's own environment (/proc/$PPID/environ) or from `end`'s command
  # line - see the README's trust note.
  echo "Running build-command..."
  local build_status=0
  (cd "$ABS_PROJECT_BASE_DIR" && env -u SONAR_TOKEN bash -c "$BUILD_COMMAND") 2>&1 | tee "$BUILD_LOG_FILE" || build_status=$?
  if (( build_status != 0 )); then
    fail build-command-failed "build-command exited with status ${build_status}; not running dotnet-sonarscanner end."
  fi

  if ! (cd "$ABS_PROJECT_BASE_DIR" && "$scanner" end "/d:sonar.token=${SONAR_TOKEN}") 2>&1 | tee "$SCANNER_LOG_FILE"; then
    if grep -q "No analyzable projects were found" "$SCANNER_LOG_FILE"; then
      fail no-analyzable-projects "dotnet-sonarscanner end failed: the build compiled no C# project it could analyze."
    fi
    fail end-failed "dotnet-sonarscanner end failed."
  fi

  # `end` exits 0 "ANALYSIS SUCCESSFUL" even when a project failed to
  # build (behind a build script that swallowed the failure) or was
  # skipped for a duplicate/invalid ProjectGuid - fail instead.
  local reason_file
  reason_file="$(mktemp "${RUNNER_TEMP:-/tmp}/dotnet-check-reason.XXXXXX")"
  if ! python3 "${SCRIPTS_DIR}/check_dotnet_projects.py" \
    --sonarqube-out "${ABS_PROJECT_BASE_DIR}/.sonarqube/out" \
    --log "$BUILD_LOG_FILE" \
    --log "$SCANNER_LOG_FILE" \
    --reason-out "$reason_file"; then
    local reason
    reason="$(cat "$reason_file")"
    rm -f "$reason_file"
    # No reason written: the check itself crashed before deciding - say
    # so, rather than claim a project was skipped.
    if [[ -z "$reason" ]]; then
      fail check-failed "check_dotnet_projects.py failed before reaching a verdict (see above)."
    fi
    fail "$reason" "Failing: not every project the build touched was analyzed (see above)."
  fi
  rm -f "$reason_file"
}

# --- scanner-mode: cli -------------------------------------------------------
scan_with_cli() {
  # Single source of truth for the sonar-scanner-cli version is the inert
  # `scanner` profile service in docker-compose.ephemeral.yml, so Dependabot
  # bumps to it don't also require editing this script.
  SCANNER_IMAGE="$(docker compose -f "$COMPOSE_FILE" --profile scanner config --images 2>/dev/null | grep 'sonar-scanner-cli' | head -1)"
  if [[ -z "$SCANNER_IMAGE" ]]; then
    echo "Could not resolve the sonar-scanner-cli image from ${COMPOSE_FILE}." >&2
    exit 1
  fi
  echo "Using scanner image: ${SCANNER_IMAGE}"

  rm -rf "${ABS_PROJECT_BASE_DIR}/.scannerwork"

  echo "Scanning ${ABS_PROJECT_BASE_DIR} as project '${PROJECT_KEY}'..."
  if ! docker run --rm \
    --network host \
    --user "$(id -u):$(id -g)" \
    -e SONAR_HOST_URL="${SONAR_HOST_URL}" \
    -e SONAR_TOKEN="${SONAR_TOKEN}" \
    -e HOME=/tmp \
    -e SONAR_USER_HOME=/tmp/sonar-cache \
    -v "${ABS_PROJECT_BASE_DIR}:/usr/src" \
    -v "${SCANNER_CACHE_DIR}:/tmp/sonar-cache" \
    -w /usr/src \
    "${SCANNER_IMAGE}" \
    -Dsonar.projectKey="${PROJECT_KEY}" \
    -Dsonar.projectBaseDir=/usr/src \
    -Dsonar.working.directory=/usr/src/.scannerwork \
    -Dproject.settings="/tmp/sonar-cache/${EMPTY_PROJECT_SETTINGS_NAME}" \
    "${EXTRA_SCANNER_ARGS[@]}" \
    2>&1 | tee "$SCANNER_LOG_FILE"; then
    fail scanner-failed "sonar-scanner-cli failed (see the scanner output above)."
  fi
  # ^ project.settings: see EMPTY_PROJECT_SETTINGS_NAME above.
  # sonar.working.directory: the current SonarScanner Engine
  #   (sonar-scanner-cli 12.x / engine 8.x) defaults this to
  #   /tmp/.scannerwork inside the container - not a path under
  #   projectBaseDir - so report-task.txt (read back from the host below)
  #   would never reach the bind mount without this override.
  # EXTRA_SCANNER_ARGS: any -D properties derived from the consumer's
  #   optional .sastrc/.sastignore files (see build_scan_config.py above),
  #   plus -Dsonar.scm.disabled=true when SCM_DISABLED=true - empty when
  #   neither file exists and SCM_DISABLED is unset.
  # tee SCANNER_LOG_FILE: the plain CLI never analyzes C#/VB.NET (indexes
  #   the files, runs no rules) and instead logs a fixed WARN line naming
  #   the language - see detect_unanalyzed_languages.py. `tee` still
  #   streams the scanner's output live to the job log while also
  #   capturing it to a file this script can grep after the fact; `set -o
  #   pipefail` above (part of `set -euo pipefail`) is what keeps `docker
  #   run`'s own exit status - not tee's - the one the `if !` above
  #   checks (failure-reason scanner-failed).
}

if [[ "$SCANNER_MODE" == "dotnet" ]]; then
  scan_with_dotnet
  report_task_file="${ABS_PROJECT_BASE_DIR}/.sonarqube/out/.sonar/report-task.txt"
else
  scan_with_cli
  report_task_file="${ABS_PROJECT_BASE_DIR}/.scannerwork/report-task.txt"
fi

# Detect languages this scan didn't actually analyze - either because the
# plain CLI indexed but couldn't analyze them (from the scanner's own log
# line above) or because they have no Community Build plugin at all, so
# the scanner never even logs about them (from a file-extension scan of
# PROJECT_BASE_DIR instead - see detect_unanalyzed_languages.py). In
# dotnet mode the log is `end`'s, which never carries the CLI's "cannot be
# analyzed" line; instead, any .cs/.vb file under PROJECT_BASE_DIR that no
# project's FilesToAnalyze lists (a solution build-command didn't build, a
# project whose restore failed behind `|| true`, a stray file) is reported
# as unanalyzed C#/VB.NET, alongside the extension scan (C, C++,
# Objective-C, Swift). This never fails the scan itself: it only produces
# a result that action.yml surfaces (warning annotation, job summary,
# report banner, Check Run/comment, and the fail-on-unanalyzed-languages
# input) after the findings and report outputs further downstream are
# written.
detect_args=(
  --scanner-log "$SCANNER_LOG_FILE"
  --project-base-dir "$ABS_PROJECT_BASE_DIR"
  --config "$SAST_CONFIG_FILE"
  --ignore-file "$SAST_IGNORE_FILE"
  --config-base-dir "$PROJECT_BASE_DIR"
  --out "$UNANALYZED_LANGUAGES_FILE"
)
if [[ "$SCANNER_MODE" == "dotnet" ]]; then
  detect_args+=(--dotnet-sonarqube-out "${ABS_PROJECT_BASE_DIR}/.sonarqube/out")
fi
python3 "${SCRIPTS_DIR}/detect_unanalyzed_languages.py" "${detect_args[@]}"
UNANALYZED_LANGUAGES="$(cat "$UNANALYZED_LANGUAGES_FILE")"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "unanalyzed-languages=${UNANALYZED_LANGUAGES}" >> "$GITHUB_OUTPUT"
fi

if [[ ! -f "$report_task_file" ]]; then
  fail no-report-task "Scanner did not produce ${report_task_file}."
fi

ce_task_id="$(sed -n 's/^ceTaskId=//p' "$report_task_file")"
if [[ -z "$ce_task_id" ]]; then
  fail no-report-task "Could not find ceTaskId in ${report_task_file}."
fi

echo "Waiting for compute engine task ${ce_task_id} to finish (timeout: ${SCAN_TIMEOUT_SECONDS}s)..."
elapsed=0
while (( elapsed < SCAN_TIMEOUT_SECONDS )); do
  task_json="$(curl -fsS --max-time 10 -u "${SONAR_TOKEN}:" "${SONAR_HOST_URL}/api/ce/task?id=${ce_task_id}")"
  task_status="$(echo "$task_json" | jq -r '.task.status // empty')"

  case "$task_status" in
    SUCCESS)
      echo "Compute engine task succeeded."
      exit 0
      ;;
    FAILED|CANCELED)
      echo "$task_json" >&2
      fail ce-task-failed "Compute engine task ended with status ${task_status}."
      ;;
    PENDING|IN_PROGRESS|"")
      # keep polling
      ;;
    *)
      echo "$task_json" >&2
      fail ce-task-failed "Unexpected compute engine task status '${task_status}'."
      ;;
  esac

  sleep "$POLL_INTERVAL_SECONDS"
  elapsed=$(( elapsed + POLL_INTERVAL_SECONDS ))
done

fail ce-task-timeout "Timed out after ${SCAN_TIMEOUT_SECONDS}s waiting for compute engine task ${ce_task_id}."
