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

The two files split along "structured, needs validation" (`.sastrc`, a
small YAML schema translated onto the underlying `sonar.*` properties) vs.
"the common case, familiar syntax" (`.sastignore`, a practical subset of
`.gitignore` syntax translated into the same `sonar.exclusions` value
`.sastrc`'s `exclusions.paths` produces) rather than folding both into one
file, so a consumer who only wants "exclude these paths" doesn't need to
learn YAML or SonarQube's own property names at all.
