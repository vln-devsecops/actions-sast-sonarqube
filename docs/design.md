# Design notes

Architecture-level rationale that doesn't belong in the README (which is a
usage guide) or in a single script's comments (because it's not tied to one
line of code). Add to this file as more such decisions come up.

## Why not `services:` for SonarQube/Postgres?

Elasticsearch (bundled in SonarQube) requires `vm.max_map_count=262144` set
on the **host** before the container starts, or it crash-loops. GitHub
Actions `services:` containers start before any job steps run, so the sysctl
can't be applied in time. Both workflows instead run
`sudo sysctl -w vm.max_map_count=262144` in a step, then
`docker compose -f docker-compose.ephemeral.yml up -d` in the next one.

## Why `.sastrc`/`.sastignore` are a schema + allowlist, not raw property passthrough

Every SonarQube noise-reduction lever this action wires up
(`sonar.exclusions`, `sonar.issue.ignore.multicriteria`, etc.) is a
scanner-level analysis property, not a persisted server concept - which
matters here specifically because the SonarQube instance is ephemeral
(torn down at the end of every job, see above): anything requiring
persisted state, like a named Quality Profile, would need to be
re-provisioned from scratch on every single run, which isn't worth the
complexity for what path exclusion and rule suppression already cover.

Letting a consumer's `.sastrc`/`.sastignore` set arbitrary `-D` properties
directly (rather than a fixed schema `scripts/build_scan_config.py`
validates against an allowlist) was considered and rejected: a raw
passthrough would let a config typo - or a deliberate change - silently
repoint `sonar.host.url`/`sonar.token`, or override
`sonar.projectKey`/`sonar.projectBaseDir`/`sonar.working.directory`, none of
which "noise reduction" should ever be able to touch. The schema simply has
no field for those, and any key/value outside it fails the job closed
rather than being silently dropped or silently applied - the same
philosophy as `fetch_findings.py`'s `_coerce_severity` fail-closed fallback.

The same reasoning applies to `sonar-project.properties`, which the
scanner would otherwise load from the project directory by itself, with
every key applied (including `sonar.scanner.*` runtime and connection
settings and analyzer settings that name executables). `run_scan.sh`
points `project.settings` at an empty file outside the project mount, and
`scripts/project_properties.py` parses the consumer's file as data (Java
`.properties` syntax, UTF-8, values trimmed - as the pinned scanner reads
it) and keeps only a short allowlist of analysis-scope keys, passed as
`-D` and merged in Python so each key is emitted once. An allowlist rather
than a blocklist, because the set of keys a scanner and its analyzers
honour changes with every release.

The two files split along "structured, needs validation" (`.sastrc`, a
small YAML schema translated onto the underlying `sonar.*` properties) vs.
"the common case, familiar syntax" (`.sastignore`, a practical subset of
`.gitignore` syntax translated into the same `sonar.exclusions` value
`.sastrc`'s `exclusions.paths` produces) rather than folding both into one
file, so a consumer who only wants "exclude these paths" doesn't need to
learn YAML or SonarQube's own property names at all.

## Scanner container isolation (read-only project mount; compose network when available)

`scripts/run_scan.sh` bind-mounts `PROJECT_BASE_DIR` into the scanner
container read-only (`:ro`). The scanner's own writes (its working
directory, its cache/JRE under `SONAR_USER_HOME`) all live under the
separate, writable `SCANNER_CACHE_DIR` mount instead - `sonar.working.directory`
is pinned to a path under that cache mount rather than under the project,
and `report-task.txt` is read back from there on the host side
(`SCANNER_CACHE_DIR/scannerwork/report-task.txt`), not from the project
directory.

By default - when `SONAR_HOST_URL` is still this action's own default
(`http://localhost:9000`) *and* a compose `sonarqube` service container
can be found, running, attached to exactly one docker network - the
scanner joins that network instead of the host network, reaching
SonarQube by its compose service name (`http://sonarqube:9000`) -
a separate, in-container-only URL from the one used host-side (the
compute-engine `curl` poll, further down the same script, and
`fetch_findings.py`, both unchanged). `docker compose` prefixes its
network names with the compose project name, which a caller could
override (e.g. `-p`/`COMPOSE_PROJECT_NAME`) - rather than assume that
prefix, the script reads the network's real name back from the running
`sonarqube` service container itself (`docker compose ... ps -a -q
sonarqube`, then `docker inspect`).

Three cases fall back to `--network host` (with a `::warning::` that it is
deprecated and will be removed in a future major version, naming the fix:
use this action's own compose stack and leave `sonar-host-url` at its
default) rather than fail the job - all of them mean "there is no compose
stack for this script to join", not "something is wrong with it":

- `sonar-host-url` is not the default - it is assumed to point somewhere
  other than this script's own ephemeral stack (which may not even be
  running under this `COMPOSE_FILE`), so the compose lookup isn't even
  attempted. The given URL is passed through to the scanner unchanged,
  reachable the same way it always was: the scanner container falls back
  to the host network, so a `localhost`/`127.0.0.1` value meaning "the
  host itself" still resolves correctly.
- the default URL, but no compose `sonarqube` container at all under this
  `COMPOSE_FILE` - the common "reached SonarQube some other way" case.
- the default URL, a caller-overridden compose project name (`-p`/
  `COMPOSE_PROJECT_NAME`) given only as a one-off flag to the separate
  `docker compose up` invocation that started the stack, not exported into
  *this* step's own environment. `docker compose` resolves the project
  name from its environment (or the compose file's own top-level `name:`)
  independently each time it runs; without that same `COMPOSE_PROJECT_NAME`
  present here, this script's own lookup resolves the project name
  differently from the one the stack actually started under, normally
  finds no matching container, and falls back - but if some *other*,
  unrelated container happens to exist under the name this script
  resolves to, the lookup can instead find it and fail closed (next
  paragraph), since "found something, but it doesn't check out" and
  "found nothing at all" aren't told apart by name alone. Export
  `COMPOSE_PROJECT_NAME` into the step itself (not only the `up` command)
  to make the lookup follow the override correctly instead.

The reusable workflows do exactly that. Because one docker daemon is often
shared by many self-hosted runners, a fixed project name and host port 9000
would make concurrent jobs collide, so each job allocates a per-run
`COMPOSE_PROJECT_NAME` (`sonar-<run id>-<attempt>-<job>`) and a free host
port (`SONAR_HOST_PORT`) in a step that writes both to `$GITHUB_ENV` before
`docker compose up`, which makes them visible to every later step.
`docker-compose.ephemeral.yml` publishes `${SONAR_HOST_PORT:-9000}:9000`, and
`run_scan.sh` treats `http://localhost:${SONAR_HOST_PORT:-9000}` as the
"default" URL for the network lookup above.

A *lookup error* - the compose `sonarqube` container IS found but isn't
running (stopped, restarting), or is attached to other than exactly one
docker network, or more than one container matches the service - fails
the job closed instead of falling back, each with its own `fail` reason
code (`sonarqube-service-not-running`, `sonarqube-network-ambiguous`,
`sonarqube-service-ambiguous` - see action.yml's `failure-reason` output):
that is evidence the lookup itself (or the stack) is broken, not evidence
there simply isn't a compose stack to join.

## Confining `project-base-dir`: workspace vs. checkout root

`run_scan.sh` resolves `PROJECT_BASE_DIR` to its physical path (`pwd -P`)
and refuses to proceed if that path lies outside a confinement root - so a
checked-out symlink is mounted (and scanned) as the real directory it
points at, never silently followed past a boundary this action means to
enforce.

Before `CHECKOUT_ROOT` existed, `GITHUB_WORKSPACE` was the only root. That
is still correct for a direct action user with one checkout: nothing else
lives in the workspace for a symlink to reach. It stopped being enough for
`sonar-pr.yml`/`sonar-baseline.yml`, though: both check the caller's own
code out into `./src`, with this action's own source (`.sast-sonarqube`)
and the fallback baseline checkout (`.baseline-checkout`) as sibling
directories under the same `GITHUB_WORKSPACE`. A symbolic link inside the
caller's checkout that resolves to another directory in the same
workspace (this action's source or the baseline checkout) would pass a
workspace-wide check, because the resolved target is still inside the
workspace - the check can't tell that apart from a legitimate path, and
would scan a directory other than the caller's checkout.

`CHECKOUT_ROOT`, when set, adds a second, narrower boundary - `src` for
the PR head and baseline scans, `.baseline-checkout` for the fallback
baseline scan - resolved physically the same way as `PROJECT_BASE_DIR`
itself. It narrows the confinement in *addition* to the `GITHUB_WORKSPACE`
check above, never in place of it: `PROJECT_BASE_DIR` has to satisfy both
checks that are set, so a `CHECKOUT_ROOT` that itself resolves outside
`GITHUB_WORKSPACE` (a symlink, or a relative value like `..`) doesn't let
anything outside the workspace through either.
`tests/test_workflow_hardening.py` pins, for every step that invokes the
scanner, that its `checkout-root` matches the literal checkout prefix in
its own `project-base-dir`, is the `path:` of an actual `actions/checkout`
step in the same job, and is never `.sast-sonarqube` (which, being this
action's own trusted source, is itself a real checkout `path:` in both
workflows - the "is a real checkout path" condition alone wouldn't catch
a mistake that points a scan step at it). Left unset - every direct
action user by default - only the `GITHUB_WORKSPACE` check applies,
unchanged from before this input existed. Outside Actions, with neither
set, there is no root to confine to and the check is skipped entirely,
same as always.
