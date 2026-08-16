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

log "Rotating default admin credential..."
change_password_status="$(curl -fsS -o /dev/null -w '%{http_code}' \
  -u 'admin:admin' \
  -X POST "${HOST_URL}/api/users/change_password" \
  --data-urlencode 'login=admin' \
  --data-urlencode 'previousPassword=admin' \
  --data-urlencode "password=${NEW_PASSWORD}")"

if [[ "$change_password_status" != "204" ]]; then
  log "Failed to change the default admin password (HTTP ${change_password_status})."
  exit 1
fi

log "Generating SonarQube user token '${TOKEN_NAME}'..."
response="$(curl -fsS \
  -u "admin:${NEW_PASSWORD}" \
  -X POST "${HOST_URL}/api/user_tokens/generate" \
  --data-urlencode "name=${TOKEN_NAME}")"

token="$(echo "$response" | jq -r '.token // empty')"
if [[ -z "$token" ]]; then
  log "Token generation did not return a token. Response: ${response}"
  exit 1
fi

log "Token '${TOKEN_NAME}' generated."
echo "$token"
