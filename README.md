# actions-sast-sonarqube

Runs [SonarQube Community Build](https://www.sonarsource.com/open-source/) —
self-hosted, OSS, ephemeral — against a repo on two triggers:

1. **push to a baseline branch** (`main`, or any branch that's a PR target
   under branch protection): full scan, findings published as a build
   artifact keyed by commit SHA.
2. **pull_request**: scans PR HEAD, diffs the result against the baseline
   findings for the PR's branch-off point, and surfaces only *new* findings
   as GitHub Check Run annotations (plus, by default, a PR summary comment).
   Can optionally fail the job on policy violation.

No persistent SonarQube server, no SonarQube Cloud, no SonarSource-hosted
product, and no reliance on SonarQube's own Quality Gate / New Code Period /
branch-analysis features - those assume persisted server state and are
partly paywalled above Community Edition anyway. New-finding detection is
done client-side, by this action, diffing two independent flat analyses.

## How it works

```
docker-compose.ephemeral.yml   SonarQube Community Build + Postgres, both
                                pinned, both ephemeral - no named volumes,
                                everything dies with `docker compose down -v`.

action.yml                     Composite action: "run one scan against a
                                checked-out directory, return normalized
                                findings JSON." Used by both workflows below
                                so scan logic lives in exactly one place.

.github/workflows/
  sonar-baseline.yml           Reusable workflow (workflow_call). Scans HEAD
                                of a push to a baseline branch, uploads
                                findings as artifact "sonar-baseline-<sha>".

  sonar-pr.yml                 Reusable workflow (workflow_call). Resolves a
                                baseline (see below), scans PR HEAD, diffs,
                                applies a blocking policy, posts a Check Run
                                + PR comment.

  ci.yml                       This repo's own CI: calls the two reusable
                                workflows above against fixtures/, on every
                                push to dev and every PR.
```

### Why not `services:` for SonarQube/Postgres?

Elasticsearch (bundled in SonarQube) requires `vm.max_map_count=262144` set
on the **host** before the container starts, or it crash-loops. GitHub
Actions `services:` containers start before any job steps run, so the sysctl
can't be applied in time. Both workflows instead run
`sudo sysctl -w vm.max_map_count=262144` in a step, then
`docker compose -f docker-compose.ephemeral.yml up -d` in the next one.

### Baseline resolution for PRs

`sonar-pr.yml` tries, in order:

1. **Tier a**: artifact `sonar-baseline-<merge-base-sha>` - the commit the PR
   actually branched off from. This is what most PRs hit, since the baseline
   workflow runs on every push to the target branch.
2. **Tier b**: artifact `sonar-baseline-<target-branch-HEAD-sha>` - a
   reasonable second choice when (1) aged out or predates the baseline
   workflow's existence.
3. **Fallback**: neither artifact exists. Scan the target branch's *current*
   HEAD live, in the same job, as the baseline (not the merge-base - simpler,
   and it's already the second-preference target anyway).

Which tier fired is recorded in the job's step summary and in the PR
comment, to help tune artifact retention later.

`actions/download-artifact` alone can only see artifacts from the *same*
run. Finding "whichever artifact happens to be named
`sonar-baseline-<sha>`" needs a name-filtered listing across the whole repo,
so `scripts/find_baseline_artifact.py` calls the REST API directly
(`GET /repos/{owner}/{repo}/actions/artifacts?name=...`).

### New-finding matching

Two findings across independent analyses are the same issue if they share
`(ruleKey, componentPath, hash)` - or, when `hash` is empty (SonarQube
returns no hash for some issue types, e.g. cross-file duplication),
`(ruleKey, componentPath, line)` instead. `hash` is a checksum of the source
line's content at issue-creation time - it's what SonarQube's own
issue-tracking engine uses internally to survive line-number churn; this
action applies it across two independent analyses instead of across one
analysis's history.

**Known limitation of hash matching** (inherited from SonarQube's own
algorithm, not specific to this action): two *textually identical* lines
triggering the same rule in the same file will hash-collide. If a PR
introduces a second, brand-new violation of a rule on a line whose content
happens to exactly match an existing baseline violation of that same rule in
that file, the new one won't be flagged. This is a rare edge case in
practice.

### Blocking policy

Our own, evaluated only against the new-findings set - not SonarQube's
Quality Gate, which is also a baseline/period concept requiring persisted
server config we deliberately don't keep. Default: a new finding at legacy
`severity` `MAJOR` or above (`MAJOR`, `CRITICAL`, `BLOCKER`) fails the job.
Configurable via `severity-threshold` / `blocking` inputs on `sonar-pr.yml`.

`severity` (not the newer `impacts[]` model) is what's evaluated by default,
because SonarQube populates it for backward compatibility regardless of
whether the instance runs in Standard or MQR analysis mode. `MAJOR` is the
positional equivalent of `impacts[].severity: MEDIUM` on the newer 5-level
scale, if that mapping matters later. `impacts[]` is still carried in the
findings JSON for context.

**Note**: only SonarQube *Issues* (bugs, code smells, vulnerabilities) are
covered - the API used (`api/issues/search`) does not return *Security
Hotspots*, which SonarQube tracks and reviews separately.

## Using this action from another repo

Both reusable workflows live in this repo and are meant to be called from a
consumer repo's own workflows:

```yaml
# consumer repo: .github/workflows/sonarqube.yml
on:
  push:
    branches: [main]
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  baseline:
    if: github.event_name == 'push'
    uses: vln-devsecops/actions-sast-sonarqube/.github/workflows/sonar-baseline.yml@main
    permissions:
      contents: read
    with:
      baseline-branches: '["main"]'

  pr-scan:
    if: github.event_name == 'pull_request'
    uses: vln-devsecops/actions-sast-sonarqube/.github/workflows/sonar-pr.yml@main
    permissions:
      contents: read
      checks: write
      pull-requests: write
      actions: read
```

If this action's repo is private, the consumer repo needs read access to it:
**Settings → Actions → General → Access** on this repo, "Accessible from
repositories in the `vln-devsecops` organization" (or equivalent) - both
reusable workflows checkout this repo's own source (pinned to the exact
ref/commit the reusable workflow itself is running from) to invoke
`action.yml`, since `uses: ./` inside a reusable workflow resolves against
whatever the *caller's* checkout populated, not this repo.

## Local development

```sh
sudo sysctl -w vm.max_map_count=262144
docker compose -f docker-compose.ephemeral.yml up -d
./scripts/wait_for_sonarqube.sh http://localhost:9000
token=$(./scripts/bootstrap_sonarqube.sh http://localhost:9000 dev)
SONAR_HOST_URL=http://localhost:9000 SONAR_TOKEN="$token" \
  PROJECT_KEY=test PROJECT_BASE_DIR=fixtures \
  COMPOSE_FILE=docker-compose.ephemeral.yml \
  ./scripts/run_scan.sh
python3 scripts/fetch_findings.py --host http://localhost:9000 --token "$token" \
  --project-key test --out /tmp/findings.json
docker compose -f docker-compose.ephemeral.yml down -v
```

## Deviations from the original spec

- **`scripts/bootstrap_sonarqube.sh`** (not in the original script list):
  SonarQube refuses most API calls from the built-in `admin` account until
  its default password has been changed, so something has to rotate it and
  mint a token before `action.yml` can authenticate. Kept as its own script
  rather than folded into `wait_for_sonarqube.sh` (which only waits) or
  `run_scan.sh` (which assumes a token already exists, per spec).
- **`scripts/find_baseline_artifact.py`** and **`scripts/post_pr_comment.py`**
  (not in the original script list): the spec calls out the need for both
  (cross-run artifact lookup via the REST API; create-or-update PR comment
  via a hidden marker) without naming a script for them.
- **sonar-scanner-cli's pinned version lives in `docker-compose.ephemeral.yml`**,
  as an inert `scanner`-profile service (`entrypoint: ["true"]`, never
  started by `docker compose up -d`'s default profile) rather than hardcoded
  in `scripts/run_scan.sh`. Dependabot's `docker` ecosystem only scans
  Dockerfiles/compose files, not arbitrary shell scripts, so this is what
  makes scanner-cli bumps arrive as reviewable PRs like the other two images.
  `run_scan.sh` reads the pinned tag back out of the compose file at run time
  (`docker compose config --images --profile scanner`) so it's declared once.

## Things found by actually running this stack, not just reading it

This repo's own smoke test (bring the compose stack up, bootstrap, scan
`fixtures/`, fetch, diff) surfaced four real issues that a read-through
wouldn't have caught, all fixed in the current scripts:

- The bundled Elasticsearch also enforces a `nofile` (max file descriptors)
  bootstrap check separate from `vm.max_map_count` - `docker-compose.ephemeral.yml`
  now sets `ulimits.nofile` to 65536 on the `sonarqube` service.
- SonarQube's default admin password policy requires a special character;
  `secrets.token_urlsafe()`'s alphabet doesn't have one. `bootstrap_sonarqube.sh`
  now builds a password guaranteed to contain upper/lower/digit/special.
- The current scanner engine (sonar-scanner-cli 12.x / SonarScanner Engine
  8.x) defaults `sonar.working.directory` to a path outside the mounted
  project directory (`/tmp/.scannerwork` in the container), so
  `report-task.txt` never reached the host. `run_scan.sh` now pins
  `sonar.working.directory` under the mounted project dir explicitly.
- The scanner image's default non-root user can't write into a bind-mounted
  directory owned by the host's checkout user, so `run_scan.sh` adds
  `--user "$(id -u):$(id -g)"`. That in turn broke the image's *own* baked-in
  cache dir (`/opt/sonar-scanner/.sonar/cache`, owned by the image's uid 1000)
  once the container is running as some other uid - this only surfaced on the
  real GitHub Actions runner (`runner`, uid 1001), because my first local
  smoke test ran everything as root, and root bypasses Unix permission
  checks entirely, silently masking the bug. Re-ran the smoke test as a
  genuine non-root user afterward to confirm the fix and catch anything else
  root had been hiding: `run_scan.sh` now redirects the cache to a
  host-created, correctly-owned directory via `SONAR_USER_HOME` /
  `-v ...:/tmp/sonar-cache`, mounted separately from the project directory
  so cache files never end up in what gets scanned.

With those fixes, a full local run against `fixtures/` (fresh instance,
pristine baseline scan vs. a modified "head" copy with one shifted-but-
unchanged finding set and one genuinely new finding) produced exactly the
expected diff: the shifted findings were correctly matched via `hash` and
excluded, and the one new finding was correctly surfaced.

## What wasn't (and couldn't be) validated end-to-end here

The scripts above were validated against a real, locally-run ephemeral
SonarQube instance. The GitHub-API-dependent pieces
(`find_baseline_artifact.py`, `apply_policy.py`'s Check Run creation,
`post_pr_comment.py`) were validated by unit-level exercise of their pure
logic (severity ordering, annotation shaping, comment rendering) plus code
review, not against the real GitHub API, since that needs a real repo/PR
context. The tier-b and fallback baseline-resolution paths, and the
end-to-end blocking-job behavior, should be exercised against a real PR
once this is merged - see the acceptance criteria in the original spec.

## Open questions

- **Artifact retention period**: left at the default 90 days
  (`artifact-retention-days` input on `sonar-baseline.yml`). Flagging back
  per the spec rather than guessing a shorter/longer value - no reason surfaced
  during implementation to deviate from the default.
