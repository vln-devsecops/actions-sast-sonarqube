#!/usr/bin/env bash
# Run sonar-scanner-cli against PROJECT_BASE_DIR and wait for the resulting
# SonarQube background compute-engine task to finish.
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

# The scanner image runs as a baked-in non-root user (uid 1000) by default,
# and its home directory (/opt/sonar-scanner/.sonar, where it caches
# downloads) is owned by that uid. We override --user to match the host's
# checkout owner so uid 1000's own home directory isn't the only writable
# path available (e.g. GitHub Actions' non-root `runner` user otherwise
# couldn't write anywhere). Give the scanner a dedicated, host-created (so
# correctly-owned) cache dir instead of letting it fall back to the image's
# built-in one. PROJECT_BASE_DIR itself is bind-mounted read-only below (see
# `:ro` on the docker run below) - the scanner only ever reads it, so its
# ownership doesn't matter for --user's purposes.
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
EXTRA_ARGS_FILE="$(mktemp)"
trap 'rm -rf "${SCANNER_CACHE_DIR}"; rm -f "${EXTRA_ARGS_FILE}"' EXIT

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

# Keep the scanner container off the host network entirely (no
# `--network host`). SONAR_HOST_URL is also used host-side, unchanged,
# further below (the compute-engine `curl` poll) - SCANNER_SONAR_HOST_URL is
# a separate, in-container-only value that may differ from it.
DEFAULT_SONAR_HOST_URL="http://localhost:9000"
SCANNER_SONAR_HOST_URL="$SONAR_HOST_URL"
SCANNER_NETWORK_ARGS=()

if [[ "$SONAR_HOST_URL" == "$DEFAULT_SONAR_HOST_URL" ]]; then
  # SONAR_HOST_URL is still this action's own default, so assume it names
  # the local ephemeral stack that COMPOSE_FILE (docker-compose.ephemeral.yml)
  # itself manages, and reach it by compose service name on that stack's
  # own docker network instead of the host network.
  #
  # `docker compose` prefixes network names with the compose project name
  # (docker-compose.ephemeral.yml's top-level `name:`, normally fixed -
  # but a caller could still override it via -p/COMPOSE_PROJECT_NAME, e.g.
  # to run two stacks side by side). Rather than assume that prefix, read
  # the network's real name back from the running `sonarqube` service
  # container itself, so this keeps working either way.
  mapfile -t sonarqube_containers < <(docker compose -f "$COMPOSE_FILE" ps -q sonarqube)
  if [[ ${#sonarqube_containers[@]} -ne 1 || -z "${sonarqube_containers[0]}" ]]; then
    echo "Expected exactly one running 'sonarqube' service container for ${COMPOSE_FILE} (SONAR_HOST_URL is this action's default, ${DEFAULT_SONAR_HOST_URL}) - is the ephemeral stack up?" >&2
    exit 1
  fi
  # Via `{{json ...}} | jq`, not a hand-rolled `{{range}}` template: `docker
  # inspect -f` appends its own trailing newline after whatever the
  # template itself prints, so a `{{println}}`-terminated range emits one
  # trailing blank line *in addition to* one line per network - silently
  # inflating a single-network result to two mapfile elements. jq's own
  # `keys[]` output has no such extra line.
  mapfile -t sonarqube_networks < <(docker inspect \
    -f '{{json .NetworkSettings.Networks}}' "${sonarqube_containers[0]}" | jq -r 'keys[]')
  if [[ ${#sonarqube_networks[@]} -ne 1 ]]; then
    echo "Expected the 'sonarqube' service container to be attached to exactly one docker network, found: ${sonarqube_networks[*]:-none}." >&2
    exit 1
  fi
  SCANNER_NETWORK_ARGS=(--network "${sonarqube_networks[0]}")
  # "sonarqube" is docker-compose.ephemeral.yml's service name - always
  # reachable under that name on its own network. 9000 is that service's
  # own container port, independent of any host-side port mapping.
  SCANNER_SONAR_HOST_URL="http://sonarqube:9000"
else
  # A caller-supplied, non-default sonar-host-url: assume it does not name
  # this script's own ephemeral stack (which this COMPOSE_FILE may not even
  # have started), so don't try to join its compose network. The scanner
  # container still never joins the host network either way - it runs on
  # docker's default bridge network instead - so this URL must be
  # reachable from a container on that network (a routable hostname/IP;
  # not "localhost" meaning the *host* itself, which the scanner container
  # can no longer reach this way).
  echo "SONAR_HOST_URL (${SONAR_HOST_URL}) is not this action's default (${DEFAULT_SONAR_HOST_URL}); running the scanner container on the default bridge network rather than joining the ephemeral stack's compose network." >&2
fi

echo "Scanning ${ABS_PROJECT_BASE_DIR} as project '${PROJECT_KEY}'..."
docker run --rm \
  "${SCANNER_NETWORK_ARGS[@]}" \
  --user "$(id -u):$(id -g)" \
  -e SONAR_HOST_URL="${SCANNER_SONAR_HOST_URL}" \
  -e SONAR_TOKEN="${SONAR_TOKEN}" \
  -e HOME=/tmp \
  -e SONAR_USER_HOME=/tmp/sonar-cache \
  -v "${ABS_PROJECT_BASE_DIR}:/usr/src:ro" \
  -v "${SCANNER_CACHE_DIR}:/tmp/sonar-cache" \
  -w /usr/src \
  "${SCANNER_IMAGE}" \
  -Dsonar.projectKey="${PROJECT_KEY}" \
  -Dsonar.projectBaseDir=/usr/src \
  -Dsonar.working.directory=/tmp/sonar-cache/scannerwork \
  -Dproject.settings="/tmp/sonar-cache/${EMPTY_PROJECT_SETTINGS_NAME}" \
  "${EXTRA_SCANNER_ARGS[@]}"
  # ^ project mount (:ro): the project is only ever read by the scanner now -
  #   every path it writes to (sonar.working.directory below, its cache/JRE
  #   under SONAR_USER_HOME) lives under the separate, writable cache mount
  #   (SCANNER_CACHE_DIR above) instead.
  # project.settings: see EMPTY_PROJECT_SETTINGS_NAME above.
  # sonar.working.directory: moved under /tmp/sonar-cache (the writable
  #   cache mount) now that /usr/src is read-only. The current SonarScanner
  #   Engine (sonar-scanner-cli 12.x / engine 8.x) defaults this to
  #   /tmp/.scannerwork inside the container - not a path under either
  #   mount - so report-task.txt (read back from the host below, at
  #   SCANNER_CACHE_DIR/scannerwork/report-task.txt) would never reach a
  #   host-visible path without this override.
  # SCANNER_NETWORK_ARGS / SCANNER_SONAR_HOST_URL: see above - keeps the
  #   scanner off the host network while still reaching SonarQube.
  # EXTRA_SCANNER_ARGS: any -D properties derived from the consumer's
  #   optional .sastrc/.sastignore files (see build_scan_config.py above),
  #   plus -Dsonar.scm.disabled=true when SCM_DISABLED=true - empty when
  #   neither file exists and SCM_DISABLED is unset.

report_task_file="${SCANNER_CACHE_DIR}/scannerwork/report-task.txt"
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
