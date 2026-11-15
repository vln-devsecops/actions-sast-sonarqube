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
#                NUGET_AUTH_TOKEN (dotnet only; optional - a private NuGet
#                feed credential build-command needs to restore. Reaches
#                build-command's subshell by plain inheritance, but IS
#                `env -u`'d from the dotnet tool install and the begin/end
#                scanner invocations - none of those restore anything, so
#                the credential has no use there, same "scoped to build
#                only" promise action.yml's nuget-auth-token input makes;
#                action.yml also keeps it out of the fetch/report steps
#                after this one)
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
# checkout owner so uid 1000's own home directory isn't the only writable
# path available (e.g. GitHub Actions' non-root `runner` user otherwise
# couldn't write anywhere). Give the scanner a dedicated, host-created (so
# correctly-owned) cache dir instead of letting it fall back to the image's
# built-in one. PROJECT_BASE_DIR itself is bind-mounted read-only in cli
# mode (see `:ro` in scan_with_cli below) - the scanner only ever reads it
# there, so its ownership doesn't matter for --user's purposes in that mode.
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

  # `dotnet-sonarscanner` (and the scanner's own JRE-launching apphost)
  # needs the .NET runtime to be findable. Per
  # https://learn.microsoft.com/dotnet/core/tools/dotnet-environment-variables#dotnet_root-dotnet_rootx86-dotnet_root_x86-dotnet_root_x64
  # an apphost looks for it in this order: DOTNET_ROOT_<ARCH> (e.g.
  # DOTNET_ROOT_X64 on an x64 process), DOTNET_ROOT(x86) (32-bit process on
  # 64-bit Windows only - not applicable on the Linux runners this action
  # targets), then DOTNET_ROOT, then the registered install location, then
  # the OS default (e.g. /usr/share/dotnet on Linux). actions/setup-dotnet
  # sets DOTNET_ROOT, so GitHub-hosted runners (and any self-hosted runner
  # that also sets it) are already covered. A self-hosted runner whose .NET
  # SDK lives somewhere non-standard (e.g. a user-local $HOME/.dotnet-csharp)
  # with neither set has nothing pointing at it, and `begin` fails with
  # "You must install .NET to run this application." even though `dotnet`
  # itself is right there on PATH.
  #
  # Only derive it when BOTH the architecture-specific variable and
  # DOTNET_ROOT itself are unset - a caller-provided value of either is
  # never overridden. When derived, it's passed as an explicit `env`
  # assignment to each of this function's own dotnet/scanner invocations
  # below (install, begin, build, end), not exported into this whole
  # script's environment, so it can't leak into anything else this script
  # runs (the cli-mode scanner container, the compute-engine poll, ...).
  local dotnet_root_arch_var=""
  case "$(uname -m)" in
    x86_64) dotnet_root_arch_var="DOTNET_ROOT_X64" ;;
    aarch64|arm64) dotnet_root_arch_var="DOTNET_ROOT_ARM64" ;;
    armv7l|armv6l) dotnet_root_arch_var="DOTNET_ROOT_ARM" ;;
    i386|i686) dotnet_root_arch_var="DOTNET_ROOT_X86" ;;
  esac
  local dotnet_root_already_set=false
  if [[ -n "${DOTNET_ROOT:-}" ]]; then
    dotnet_root_already_set=true
  elif [[ -n "$dotnet_root_arch_var" && -n "${!dotnet_root_arch_var:-}" ]]; then
    dotnet_root_already_set=true
  fi
  local DOTNET_ROOT_ENV_ARGS=()
  if [[ "$dotnet_root_already_set" == false ]]; then
    local dotnet_cmd
    dotnet_cmd="$(command -v dotnet || true)"
    if [[ -n "$dotnet_cmd" ]]; then
      # readlink -f: resolve through a symlinked `dotnet` (e.g. from
      # update-alternatives or a version manager) to its physical install
      # directory, not the symlink's own directory.
      DOTNET_ROOT_ENV_ARGS=(DOTNET_ROOT="$(dirname "$(readlink -f "$dotnet_cmd")")")
    fi
    # else: dotnet isn't on PATH at all - leave DOTNET_ROOT_ENV_ARGS empty
    # and fall through unchanged; the `dotnet tool install` below fails the
    # same way it always did (scanner-install-failed).
  fi

  DOTNET_TOOL_DIR="$(mktemp -d "${RUNNER_TEMP:-/tmp}/dotnet-sonarscanner.XXXXXX")"
  BUILD_LOG_FILE="$(mktemp "${RUNNER_TEMP:-/tmp}/dotnet-build-log.XXXXXX")"
  echo "Installing dotnet-sonarscanner ${version}..."
  # env -u NUGET_AUTH_TOKEN: this installs OUR OWN pinned tool, not the
  # code under scan - scope the credential to build-command only (its
  # description promises "build only"), the same direction SONAR_TOKEN is
  # already scoped away from build-command below.
  if ! env -u NUGET_AUTH_TOKEN "${DOTNET_ROOT_ENV_ARGS[@]}" dotnet tool install dotnet-sonarscanner --version "$version" --tool-path "$DOTNET_TOOL_DIR"; then
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
  #   scanner's own guess (since v8: the projects' common root). Dotnet mode
  #   runs on the runner's own host network, unaffected by cli mode's
  #   container network handling below - sonar.host.url is passed through
  #   as-is either way.
  # env -u NUGET_AUTH_TOKEN on begin/end too: neither runs build-command or
  # restores anything, so the credential has no use there - keep it scoped
  # to build-command only, matching the secret's own "build only" promise
  # (action.yml's nuget-auth-token input description).
  if ! (cd "$ABS_PROJECT_BASE_DIR" && env -u NUGET_AUTH_TOKEN "${DOTNET_ROOT_ENV_ARGS[@]}" "$scanner" begin \
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
  (cd "$ABS_PROJECT_BASE_DIR" && env -u SONAR_TOKEN "${DOTNET_ROOT_ENV_ARGS[@]}" bash -c "$BUILD_COMMAND") 2>&1 | tee "$BUILD_LOG_FILE" || build_status=$?
  if (( build_status != 0 )); then
    fail build-command-failed "build-command exited with status ${build_status}; not running dotnet-sonarscanner end."
  fi

  if ! (cd "$ABS_PROJECT_BASE_DIR" && env -u NUGET_AUTH_TOKEN "${DOTNET_ROOT_ENV_ARGS[@]}" "$scanner" end "/d:sonar.token=${SONAR_TOKEN}") 2>&1 | tee "$SCANNER_LOG_FILE"; then
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

  # No `rm -rf .../.scannerwork` here: the scanner can no longer create one
  # under PROJECT_BASE_DIR at all (the mount below is `:ro`, and
  # sonar.working.directory is pinned under SCANNER_CACHE_DIR instead), so
  # there is nothing of this script's own making left to clean up there -
  # and writing to the project tree, even just to delete a leftover
  # directory, is exactly what the read-only mount means to avoid.

  # Keep the scanner container off the host network when this script's own
  # ephemeral stack (docker-compose.ephemeral.yml, via COMPOSE_FILE) can be
  # found running - joining its compose network instead and reaching
  # SonarQube by compose service name. `--network host` is kept as an
  # explicit, deprecated FALLBACK (not an error) for two cases v1 callers
  # already rely on: a non-default sonar-host-url (pointing at *some*
  # SonarQube, not necessarily this script's own compose stack), and the
  # default URL with no compose stack this script itself can find (e.g. a
  # bare `docker run -p 9000:9000 sonarqube`, see docs/design.md). A real
  # lookup error once the compose container IS found - present but not
  # running, or on other than exactly one network - fails closed instead
  # of silently falling back, since that's evidence something about the
  # lookup itself is broken, not evidence there's no compose stack at all.
  #
  # SONAR_HOST_URL is also used host-side, unchanged, further below (the
  # compute-engine `curl` poll) - SCANNER_SONAR_HOST_URL is a separate,
  # in-container-only value that may differ from it.
  DEFAULT_SONAR_HOST_URL="http://localhost:9000"
  SCANNER_SONAR_HOST_URL="$SONAR_HOST_URL"
  SCANNER_NETWORK_ARGS=(--network host)
  USE_HOST_NETWORK=true

  if [[ "$SONAR_HOST_URL" == "$DEFAULT_SONAR_HOST_URL" ]]; then
    # `docker compose` prefixes network names with the compose project name
    # (docker-compose.ephemeral.yml's top-level `name:`, normally fixed -
    # but a caller could still override it via -p/COMPOSE_PROJECT_NAME).
    # Rather than assume that prefix, read the network's real name back
    # from the running `sonarqube` service container itself - this also
    # means the lookup below only follows a -p/COMPOSE_PROJECT_NAME
    # override when it's exported into THIS step's own environment, not
    # only passed to a separate `docker compose up` invocation earlier:
    # `docker compose` resolves the project name from its own environment
    # independently each time it runs, so without that same override
    # present here, this lookup resolves a different project name than
    # the stack actually started under - normally finding no container at
    # all (falls back to --network host below, see docs/design.md), but
    # finding and failing closed on some unrelated container instead if
    # one happens to exist under the name this lookup resolves to.
    #
    # `ps -a -q` (not plain `ps -q`, which silently omits a stopped
    # container): "no container at all" and "a container that exists but
    # isn't running" need to be told apart below - the first is this
    # script's own fallback case, the second is the fail-closed case.
    #
    # Captured via `$(...)` with an explicit `||`, not piped straight into
    # `mapfile < <(...)`: a process substitution's own exit status isn't
    # checked there, so a failing `docker compose ps` (docker/compose
    # itself broken) would otherwise look exactly like "ran fine, found
    # nothing" and silently turn host networking back on instead of
    # surfacing the broken lookup.
    sonarqube_ps_output="$(docker compose -f "$COMPOSE_FILE" ps -a -q sonarqube)" \
      || fail sonarqube-lookup-failed "Could not list the compose 'sonarqube' service container for ${COMPOSE_FILE} (docker compose ps failed; see the error above)."
    mapfile -t sonarqube_containers <<< "$sonarqube_ps_output"
    # A real "no matching container" prints nothing at all (zero lines).
    # A single blank line (one empty-string element) means the same thing
    # in practice (no container id to act on) - fold it into the same
    # "not found" case rather than treating it as a found-but-unusable id.
    if [[ ${#sonarqube_containers[@]} -eq 1 && -z "${sonarqube_containers[0]}" ]]; then
      sonarqube_containers=()
    fi
    case "${#sonarqube_containers[@]}" in
      0)
        # No compose container at all for this COMPOSE_FILE: not this
        # script's own ephemeral stack (never started, or SonarQube
        # reached some other way under the same default URL) - fall back
        # to the host network below, same as a non-default sonar-host-url.
        ;;
      1)
        # A failing `docker inspect` here (the container disappeared
        # between `ps` and here, or docker itself is broken) is folded
        # into the same reason as "found but not running" - either way,
        # a failure-reason is always written rather than this script
        # dying under `set -e` with no reason code at all.
        sonarqube_status="$(docker inspect -f '{{.State.Status}}' "${sonarqube_containers[0]}")" \
          || fail sonarqube-service-not-running "Could not inspect the compose 'sonarqube' service container for ${COMPOSE_FILE} (docker inspect failed; see the error above)."
        if [[ "$sonarqube_status" != "running" ]]; then
          fail sonarqube-service-not-running "The compose 'sonarqube' service container for ${COMPOSE_FILE} exists but is not running (status: ${sonarqube_status})."
        fi
        # Via `{{json ...}} | jq`, not a hand-rolled `{{range}}` template:
        # `docker inspect -f` appends its own trailing newline after
        # whatever the template itself prints, so a
        # `{{println}}`-terminated range emits one trailing blank line *in
        # addition to* one line per network - silently inflating a
        # single-network result to two mapfile elements. jq's own
        # `keys[]` output has no such extra line.
        mapfile -t sonarqube_networks < <(docker inspect \
          -f '{{json .NetworkSettings.Networks}}' "${sonarqube_containers[0]}" | jq -r 'keys[]')
        if [[ ${#sonarqube_networks[@]} -ne 1 ]]; then
          fail sonarqube-network-ambiguous "Expected the running compose 'sonarqube' service container to be attached to exactly one docker network, found: ${sonarqube_networks[*]:-none}."
        fi
        SCANNER_NETWORK_ARGS=(--network "${sonarqube_networks[0]}")
        USE_HOST_NETWORK=false
        # "sonarqube" is docker-compose.ephemeral.yml's service name -
        # always reachable under that name on its own network. 9000 is
        # that service's own container port, independent of any host-side
        # port mapping.
        SCANNER_SONAR_HOST_URL="http://sonarqube:9000"
        ;;
      *)
        fail sonarqube-service-ambiguous "Found ${#sonarqube_containers[@]} containers for the compose 'sonarqube' service for ${COMPOSE_FILE}, expected at most one."
        ;;
    esac
  fi

  if [[ "$USE_HOST_NETWORK" == "true" ]]; then
    echo "::warning::Running the scanner container on the host network is deprecated and will be removed in a future major version. To avoid this, use this action's own compose stack (docker-compose.ephemeral.yml) and leave sonar-host-url at its default (${DEFAULT_SONAR_HOST_URL}), so the scanner can join its network instead."
  fi

  echo "Scanning ${ABS_PROJECT_BASE_DIR} as project '${PROJECT_KEY}'..."
  if ! docker run --rm \
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
    "${EXTRA_SCANNER_ARGS[@]}" \
    2>&1 | tee "$SCANNER_LOG_FILE"; then
    fail scanner-failed "sonar-scanner-cli failed (see the scanner output above)."
  fi
  # ^ project mount (:ro): the project is only ever read by the scanner now -
  #   every path it writes to (sonar.working.directory below, its cache/JRE
  #   under SONAR_USER_HOME) lives under the separate, writable cache mount
  #   (SCANNER_CACHE_DIR above) instead, regardless of which network branch
  #   above was taken.
  # project.settings: see EMPTY_PROJECT_SETTINGS_NAME above.
  # sonar.working.directory: moved under /tmp/sonar-cache (the writable
  #   cache mount) now that /usr/src is read-only. The current SonarScanner
  #   Engine (sonar-scanner-cli 12.x / engine 8.x) defaults this to
  #   /tmp/.scannerwork inside the container - not a path under either
  #   mount - so report-task.txt (read back from the host below, at
  #   SCANNER_CACHE_DIR/scannerwork/report-task.txt) would never reach a
  #   host-visible path without this override.
  # SCANNER_NETWORK_ARGS / SCANNER_SONAR_HOST_URL: see above.
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
  report_task_file="${SCANNER_CACHE_DIR}/scannerwork/report-task.txt"
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
# Objective-C, Swift). Finding unanalyzed languages never fails the scan
# itself: it only produces a result that action.yml surfaces (warning
# annotation, job summary, report banner, Check Run/comment, and the
# fail-on-unanalyzed-languages input) after the findings and report
# outputs further downstream are written.
# Detection does fail the scan if it can't read .sastrc/.sastignore safely:
# it re-reads them through read_config_file(), and a file that has become a
# symbolic link (or otherwise invalid) since build_scan_config.py read it -
# in dotnet mode, build-command runs in between and could swap one - is
# rejected the same way, rather than silently detecting without the
# consumer's exclusions.
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
if ! python3 "${SCRIPTS_DIR}/detect_unanalyzed_languages.py" "${detect_args[@]}"; then
  fail invalid-sast-config "Rejected the SAST config while detecting unanalyzed languages (see the error above)."
fi
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
