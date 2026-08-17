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

See [`docs/design.md`](docs/design.md) for architecture-level rationale
(e.g. why the SonarQube/Postgres stack isn't a `services:` block).

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
Hotspots*, which SonarQube tracks and reviews separately. Tracked as a gap
to close, not an accepted limitation: [#2](https://github.com/vln-devsecops/actions-sast-sonarqube/issues/2).

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

`@main` above is a placeholder: once this repo adopts release-please (or
equivalent) tagging, pin consumers to a version tag (e.g. `@v1`) instead.

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

## Validation status

Rationale for specific implementation choices (the ones that only surfaced by
actually running the stack, not just reading it) lives as comments next to
the code they affect - see `docker-compose.ephemeral.yml`'s `ulimits` block,
`bootstrap_sonarqube.sh`'s password generator, and `run_scan.sh`'s
`sonar.working.directory` / `SONAR_USER_HOME` handling.

The pure-logic functions in each script (`scripts/*.py`) have a pytest suite
(`tests/`), gated on 95% coverage via `vln-devsecops/actions-validate-coverage`
in `ci.yml`. Network/CLI glue is deliberately excluded from that gate and
validated live instead, the same way the rest of the pipeline is.

The full pipeline (scan → fetch → diff → policy) has been exercised against
real PRs twice, beyond the local ephemeral-instance run (`fixtures/` baseline
vs. a modified "head" copy - hash-based matching correctly excluded
shifted-but-unchanged findings and surfaced the one genuinely new one):

- PR #1 exercised the fallback baseline-resolution path (no artifact existed
  yet for a first-ever PR) and the blocking-job-failure path (`fixtures/`
  itself trips 5 findings, 4 of them MAJOR-or-above).
- PR #4 exercised tier a artifact-based baseline resolution end-to-end, once
  a baseline artifact from PR #1's merge existed to resolve against. That run
  caught a real bug on first exercise: the artifact download's redirect to
  blob storage rejected a forwarded GitHub `Authorization` header with a 401.

Not yet exercised against a real PR: tier b artifact-based baseline
resolution specifically (needs the merge-base artifact to be missing or
expired while the target branch's current-HEAD artifact still exists).

Artifact retention defaults to 90 days (`artifact-retention-days` input on
`sonar-baseline.yml`) - no reason surfaced to deviate from that.
