#!/usr/bin/env bash
# One-time bootstrap of a freshly-started, ephemeral SonarQube instance:
# rotate the default admin/admin credential (SonarQube refuses most API
# calls from the built-in admin account until its password has been
# changed) and mint a user token for the rest of the job to use.
#
# Usage: bootstrap_sonarqube.sh <host-url> [token-name]
# Prints the generated token, and only the token, to stdout. Everything else
# goes to stderr, so callers can safely do: token=$(bootstrap_sonarqube.sh ...)
set -euo pipefail

HOST_URL="${1:?usage: bootstrap_sonarqube.sh <host-url> [token-name]}"
TOKEN_NAME="${2:-gha-ephemeral-$(date +%s)}"

# SonarQube's default password policy requires at least one uppercase,
# one lowercase, one digit and one special character - plain
# secrets.token_urlsafe() output can (and did, in testing) fail that check
# since its alphabet has no special characters.
NEW_PASSWORD="$(python3 -c '
import secrets
import string

special = "!@#$%^&*()-_=+"
required = [
    secrets.choice(string.ascii_uppercase),
    secrets.choice(string.ascii_lowercase),
    secrets.choice(string.digits),
    secrets.choice(special),
]
rest_alphabet = string.ascii_letters + string.digits + special
rest = [secrets.choice(rest_alphabet) for _ in range(28)]
password = required + rest
secrets.SystemRandom().shuffle(password)
print("".join(password))
')"

log() { echo "$@" >&2; }

# SonarQube reports UP as soon as its web layer answers, but the compute
# engine and any pending DB migration can still be settling - a request that
# is accepted and then stalls would otherwise hang the whole job. Matches the
# --max-time used in run_scan.sh and wait_for_sonarqube.sh.
CURL_MAX_TIME=30

log "Rotating default admin credential..."
# Credentials are passed via a --config file read from stdin rather than -u /
# --data-urlencode on the command line, so they never appear in this
# process's argv (and therefore /proc/*/cmdline).
change_password_status="$(printf '%s\n' \
  "--user admin:admin" \
  "--data-urlencode login=admin" \
  "--data-urlencode previousPassword=admin" \
  "--data-urlencode password=${NEW_PASSWORD}" \
  | curl -fsS --max-time "${CURL_MAX_TIME}" -o /dev/null -w '%{http_code}' \
      --config - -X POST "${HOST_URL}/api/users/change_password")"

if [[ "$change_password_status" != "204" ]]; then
  log "Failed to change the default admin password (HTTP ${change_password_status})."
  exit 1
fi

log "Generating SonarQube user token '${TOKEN_NAME}'..."
response="$(printf '%s\n' \
  "--user admin:${NEW_PASSWORD}" \
  "--data-urlencode name=${TOKEN_NAME}" \
  | curl -fsS --max-time "${CURL_MAX_TIME}" \
      --config - -X POST "${HOST_URL}/api/user_tokens/generate")"

token="$(echo "$response" | jq -r '.token // empty')"
if [[ -z "$token" ]]; then
  log "Token generation did not return a token. Response: ${response}"
  exit 1
fi

log "Token '${TOKEN_NAME}' generated."
echo "$token"
