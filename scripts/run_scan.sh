#!/usr/bin/env bash
# Run sonar-scanner-cli against PROJECT_BASE_DIR and wait for the resulting
# SonarQube background compute-engine task to finish. Also detects
# languages this `cli`-mode scan didn't actually analyze (see
# detect_unanalyzed_languages.py) and writes an `unanalyzed-languages`
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
#                set "true" to pass -Dsonar.scm.disabled=true, for a shallow
#                checkout where blame data isn't available)
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

if [[ ! -d "$PROJECT_BASE_DIR" ]]; then
  echo "PROJECT_BASE_DIR '${PROJECT_BASE_DIR}' does not exist or is not a directory." >&2
  exit 1
fi
# Resolve to the physical path (pwd -P), so what gets bind-mounted below is
# exactly the directory that was checked, not a symbolic link to it.
ABS_PROJECT_BASE_DIR="$(cd "$PROJECT_BASE_DIR" && pwd -P)"

# Under GitHub Actions, the directory to scan must be inside the workspace.
# Outside Actions (e.g. a manual local run) there is no workspace to
# confine to, so this check is skipped.
if [[ -n "${GITHUB_WORKSPACE:-}" ]]; then
  if ! ABS_WORKSPACE="$(cd "$GITHUB_WORKSPACE" && pwd -P)"; then
    echo "GITHUB_WORKSPACE '${GITHUB_WORKSPACE}' is not an accessible directory." >&2
    exit 1
  fi
  if [[ "$ABS_PROJECT_BASE_DIR" != "$ABS_WORKSPACE" && "$ABS_PROJECT_BASE_DIR" != "$ABS_WORKSPACE"/* ]]; then
    echo "PROJECT_BASE_DIR '${PROJECT_BASE_DIR}' resolves to '${ABS_PROJECT_BASE_DIR}', outside GITHUB_WORKSPACE '${ABS_WORKSPACE}'." >&2
    exit 1
  fi
fi

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
trap 'rm -rf "${SCANNER_CACHE_DIR}"; rm -f "${EXTRA_ARGS_FILE}" "${SCANNER_LOG_FILE}" "${UNANALYZED_LANGUAGES_FILE}"' EXIT

# The scanner would otherwise load sonar-project.properties from the
# project directory by itself, with every key in it applied. Point its
# project.settings at an empty file outside the project mount instead;
# build_scan_config.py reads the consumer's file as data and passes on only
# the supported keys (see project_properties.py).
EMPTY_PROJECT_SETTINGS_NAME="empty-sonar-project.properties"
: > "${SCANNER_CACHE_DIR}/${EMPTY_PROJECT_SETTINGS_NAME}"

# Translate the consumer's optional .sastrc/.sastignore noise-reduction
# files (neither need exist) into extra sonar-scanner -D properties. This
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
python3 "$(dirname "${BASH_SOURCE[0]}")/build_scan_config.py" "${build_scan_config_args[@]}"
readarray -d '' -t EXTRA_SCANNER_ARGS < "$EXTRA_ARGS_FILE"

echo "Scanning ${ABS_PROJECT_BASE_DIR} as project '${PROJECT_KEY}'..."
docker run --rm \
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
  2>&1 | tee "$SCANNER_LOG_FILE"
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
  #   run`'s own exit status - not tee's - the one that can fail this
  #   script, same as before this feature existed.

# Detect languages this scan didn't actually analyze - either because the
# plain CLI indexed but couldn't analyze them (from the scanner's own log
# line above) or because they have no Community Build plugin at all, so
# the scanner never even logs about them (from a file-extension scan of
# PROJECT_BASE_DIR instead - see detect_unanalyzed_languages.py). Finding
# unanalyzed languages never fails the scan itself: it only produces a
# result that action.yml surfaces (warning annotation, job summary, report
# banner, Check Run/comment, and the fail-on-unanalyzed-languages input)
# after the findings and report outputs further downstream are written.
# Detection does fail the scan if it can't read .sastrc/.sastignore safely:
# it re-reads them through read_config_file(), and a file that has become a
# symbolic link (or otherwise invalid) since build_scan_config.py read it
# is rejected the same way, rather than silently detecting without the
# consumer's exclusions.
if ! python3 "$(dirname "${BASH_SOURCE[0]}")/detect_unanalyzed_languages.py" \
  --scanner-log "$SCANNER_LOG_FILE" \
  --project-base-dir "$ABS_PROJECT_BASE_DIR" \
  --config "$SAST_CONFIG_FILE" \
  --ignore-file "$SAST_IGNORE_FILE" \
  --config-base-dir "$PROJECT_BASE_DIR" \
  --out "$UNANALYZED_LANGUAGES_FILE"; then
  echo "Rejected the SAST config while detecting unanalyzed languages (see the error above)." >&2
  exit 1
fi
UNANALYZED_LANGUAGES="$(cat "$UNANALYZED_LANGUAGES_FILE")"
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  echo "unanalyzed-languages=${UNANALYZED_LANGUAGES}" >> "$GITHUB_OUTPUT"
fi

report_task_file="${ABS_PROJECT_BASE_DIR}/.scannerwork/report-task.txt"
if [[ ! -f "$report_task_file" ]]; then
  echo "Scanner did not produce ${report_task_file}." >&2
  exit 1
fi

ce_task_id="$(sed -n 's/^ceTaskId=//p' "$report_task_file")"
if [[ -z "$ce_task_id" ]]; then
  echo "Could not find ceTaskId in ${report_task_file}." >&2
  exit 1
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
      echo "Compute engine task ended with status ${task_status}." >&2
      echo "$task_json" >&2
      exit 1
      ;;
    PENDING|IN_PROGRESS|"")
      # keep polling
      ;;
    *)
      echo "Unexpected compute engine task status '${task_status}'." >&2
      echo "$task_json" >&2
      exit 1
      ;;
  esac

  sleep "$POLL_INTERVAL_SECONDS"
  elapsed=$(( elapsed + POLL_INTERVAL_SECONDS ))
done

echo "Timed out after ${SCAN_TIMEOUT_SECONDS}s waiting for compute engine task ${ce_task_id}." >&2
exit 1
