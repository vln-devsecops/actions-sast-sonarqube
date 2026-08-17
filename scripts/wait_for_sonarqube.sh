#!/usr/bin/env bash
# Poll a SonarQube instance's /api/system/status until it reports UP.
#
# Usage: wait_for_sonarqube.sh [host-url] [timeout-seconds]
# Both arguments fall back to $SONAR_HOST_URL / $SONAR_WAIT_TIMEOUT_SECONDS,
# then to hardcoded defaults.
set -euo pipefail

HOST_URL="${1:-${SONAR_HOST_URL:-http://localhost:9000}}"
TIMEOUT_SECONDS="${2:-${SONAR_WAIT_TIMEOUT_SECONDS:-300}}"
POLL_INTERVAL_SECONDS=5

echo "Waiting for SonarQube at ${HOST_URL} to report UP (timeout: ${TIMEOUT_SECONDS}s)..."

elapsed=0
last_status=""
while (( elapsed < TIMEOUT_SECONDS )); do
  status="$(curl -fsS --max-time 10 "${HOST_URL}/api/system/status" 2>/dev/null | jq -r '.status // empty' 2>/dev/null || true)"

  if [[ "$status" == "UP" ]]; then
    echo "SonarQube is UP after ${elapsed}s."
    exit 0
  fi

  if [[ "$status" != "$last_status" ]]; then
    echo "  [${elapsed}s] status=${status:-<unreachable>}"
    last_status="$status"
  fi

  # DOWN is not a transient boot state (unlike STARTING / DB_MIGRATION_*) -
  # fail fast instead of waiting out the full timeout.
  if [[ "$status" == "DOWN" ]]; then
    echo "SonarQube reported DOWN - not a transient startup state, giving up." >&2
    exit 1
  fi

  sleep "$POLL_INTERVAL_SECONDS"
  elapsed=$(( elapsed + POLL_INTERVAL_SECONDS ))
done

echo "Timed out after ${TIMEOUT_SECONDS}s waiting for SonarQube to come UP (last status: ${last_status:-<unreachable>})." >&2
exit 1
