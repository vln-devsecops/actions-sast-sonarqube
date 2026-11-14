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

## Scanner container isolation (read-only project mount, no host network)

`scripts/run_scan.sh` bind-mounts `PROJECT_BASE_DIR` into the scanner
container read-only (`:ro`). The scanner's own writes (its working
directory, its cache/JRE under `SONAR_USER_HOME`) all live under the
separate, writable `SCANNER_CACHE_DIR` mount instead - `sonar.working.directory`
is pinned to a path under that cache mount rather than under the project,
and `report-task.txt` is read back from there on the host side
(`SCANNER_CACHE_DIR/scannerwork/report-task.txt`), not from the project
directory.

The scanner container also never joins the host network (no
`--network host`). When `SONAR_HOST_URL` is still this action's own
default (`http://localhost:9000`), the script assumes it names the local
ephemeral stack `docker-compose.ephemeral.yml` itself manages, and instead
joins that stack's own docker network, reaching SonarQube by its compose
service name (`http://sonarqube:9000`) - a separate, in-container-only URL
from the one used host-side (the compute-engine `curl` poll, further down
the same script, and `fetch_findings.py`, both unchanged). `docker compose`
prefixes its network names with the compose project name, which a caller
could override (e.g. `-p`/`COMPOSE_PROJECT_NAME`, to run two stacks side by
side) - rather than assume that prefix, the script reads the network's
real name back from the running `sonarqube` service container itself
(`docker compose ... ps -q sonarqube`, then `docker inspect`).

A caller that passes a non-default `sonar-host-url` is assumed to be
pointing somewhere other than this script's own ephemeral stack (which
may not even be running under this `COMPOSE_FILE`), so the script doesn't
try to join a compose network in that case - the scanner container still
only ever runs on docker's default bridge network, never the host
network, and the given URL is passed through to it unchanged. That means
such a URL must already be reachable from a container on the default
bridge network (a routable hostname/IP) - a bare `localhost`/`127.0.0.1`
value meaning "the host itself" would no longer resolve the way it did
when the scanner ran with `--network host`. No caller in this repo passes
a non-default value today; this is the explicit trade-off chosen if one
ever does, rather than keep the host network open for that case.
