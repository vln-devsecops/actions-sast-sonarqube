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
