#!/usr/bin/env bash
# Run sonar-scanner-cli against PROJECT_BASE_DIR and wait for the resulting
# SonarQube background compute-engine task to finish.
#
# Required env: SONAR_HOST_URL, SONAR_TOKEN, PROJECT_KEY, PROJECT_BASE_DIR,
#                COMPOSE_FILE (path to docker-compose.ephemeral.yml, used only
#                to read back the pinned scanner image tag)
# Optional env:  SCAN_TIMEOUT_SECONDS (default 600)
set -euo pipefail

: "${SONAR_HOST_URL:?SONAR_HOST_URL is required}"
: "${SONAR_TOKEN:?SONAR_TOKEN is required}"
: "${PROJECT_KEY:?PROJECT_KEY is required}"
: "${PROJECT_BASE_DIR:?PROJECT_BASE_DIR is required}"
: "${COMPOSE_FILE:?COMPOSE_FILE is required}"

SCAN_TIMEOUT_SECONDS="${SCAN_TIMEOUT_SECONDS:-600}"
POLL_INTERVAL_SECONDS=5

if [[ ! -d "$PROJECT_BASE_DIR" ]]; then
  echo "PROJECT_BASE_DIR '${PROJECT_BASE_DIR}' does not exist or is not a directory." >&2
  exit 1
fi
ABS_PROJECT_BASE_DIR="$(cd "$PROJECT_BASE_DIR" && pwd)"

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
SCANNER_CACHE_DIR="$(mktemp -d)"
trap 'rm -rf "${SCANNER_CACHE_DIR}"' EXIT

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
  -Dsonar.working.directory=/usr/src/.scannerwork

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
