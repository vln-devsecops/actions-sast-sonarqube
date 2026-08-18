# Test procedure

Manual steps to validate this action end to end. The automated coverage is
`tests/` (pytest, pure-logic functions only) plus `ci.yml` itself, which
exercises the real pipeline against `fixtures/` on every push to `dev` and
every PR - this document is for exercising the paths that don't fire on
every ordinary run, and for a human to work through when validating a
change that touches the scan/diff/policy pipeline itself.

## Prerequisites

- Docker, Python 3.12, `jq`, `shellcheck`
- Write access to this repo (to push test branches/PRs)
- For the artifact-deletion steps below: a token or UI access with
  permission to delete Actions artifacts. A plain `GITHUB_TOKEN` from a
  workflow run is *not* enough - deleting artifacts needs a user token or
  UI access; expect `403 Resource not accessible by integration` otherwise.

## 1. Unit tests (local, fast, no Docker)

```sh
pip install -r requirements-dev.txt
pytest tests/ -v --cov=scripts --cov-report=term-missing
shellcheck scripts/*.sh
```

Expect all tests to pass and every file in the coverage table at 100% -
network/CLI-glue functions are marked `# pragma: no cover` deliberately (see
each script), so the gate is meaningful rather than padded. Reproduce the
CI gate itself with `--cov-report=xml:coverage.xml`, then run
`vln-devsecops/actions-validate-coverage`'s logic against it (or just trust
the `unit-tests` job in `ci.yml` - it runs the same command).

## 2. Full pipeline against a live ephemeral instance (local)

```sh
sudo sysctl -w vm.max_map_count=262144
docker compose -f docker-compose.ephemeral.yml up -d
./scripts/wait_for_sonarqube.sh http://localhost:9000
token=$(./scripts/bootstrap_sonarqube.sh http://localhost:9000 test)

# "baseline" scan
SONAR_HOST_URL=http://localhost:9000 SONAR_TOKEN="$token" \
  PROJECT_KEY=test-baseline PROJECT_BASE_DIR=fixtures \
  COMPOSE_FILE=docker-compose.ephemeral.yml \
  ./scripts/run_scan.sh
python3 scripts/fetch_findings.py --host http://localhost:9000 --token "$token" \
  --project-key test-baseline --out /tmp/baseline-findings.json

# "head" scan - copy fixtures/, tweak it, scan under a different project key
cp -r fixtures /tmp/fixtures-head
# ...edit /tmp/fixtures-head to add/shift findings (see step 5 below)...
SONAR_HOST_URL=http://localhost:9000 SONAR_TOKEN="$token" \
  PROJECT_KEY=test-head PROJECT_BASE_DIR=/tmp/fixtures-head \
  COMPOSE_FILE=docker-compose.ephemeral.yml \
  ./scripts/run_scan.sh
python3 scripts/fetch_findings.py --host http://localhost:9000 --token "$token" \
  --project-key test-head --out /tmp/head-findings.json

python3 scripts/diff_findings.py --baseline /tmp/baseline-findings.json \
  --head /tmp/head-findings.json --out /tmp/new-findings.json
cat /tmp/new-findings.json

docker compose -f docker-compose.ephemeral.yml down -v
```

`scripts/apply_policy.py` and `scripts/post_pr_comment.py` need a real
`GITHUB_TOKEN` and post to a real repo/PR (a Check Run, a comment) - don't
run those against this repo directly outside of an actual PR; use a scratch
repo, or just trust the unit tests for their logic and section 7 below for
the live check.

## 3. Verifying the pagination offset ceiling (`fetch_findings.py`)

`scripts/fetch_findings.py` partitions `api/issues/search` queries by
severity/type once a query's `paging.total` exceeds 10,000 results
(`MAX_SEARCH_OFFSET`), because index-backed search endpoints have
historically refused to page past that offset. Whether *this* SonarQube
version still enforces that ceiling hasn't been confirmed live (see #18) -
this generates enough findings to find out:

1. Bring up the ephemeral instance as in section 2, but scan a fixture large
   enough to produce >10,000 open issues under one project key (the
   `fixtures/` tree as checked in won't get there - use a generated/vendored
   tree, or lower rule severities so more of it triggers).
2. Confirm one page still succeeds unpartitioned near the boundary:
   ```sh
   curl -u "$token": "http://localhost:9000/api/issues/search?componentKeys=test-large&ps=500&p=20"
   ```
   (`p=20` is offset 9,500-10,000, the last page `_fetch_pages` will request
   before the ceiling.)
3. Request one page past it and record what comes back:
   ```sh
   curl -u "$token": "http://localhost:9000/api/issues/search?componentKeys=test-large&ps=500&p=21"
   ```
   - If this errors (historically `Can return only the first 10000 results`),
     the ceiling is confirmed real - `_fetch_exhaustive`'s partitioning is
     load-bearing, not just a safety rail.
   - If this succeeds, the ceiling is gone on this version - partitioning is
     a no-op guard on the happy path, and `MAX_PAGES`/`MAX_SEARCH_OFFSET` can
     stay as-is without risk.
4. Run `scripts/fetch_findings.py` against `test-large` directly and confirm
   its finding count matches `paging.total` from step 2/3's response
   (i.e. nothing was silently dropped), and check stderr for the `note:
   ... splitting the query by severities` message confirming partitioning
   fired.
5. Record the result of step 3 (ceiling confirmed present/absent, and the
   SonarQube version tested) here:

   - _(not yet verified - see above)_

## 4. Baseline publish (`sonar-baseline.yml`, push trigger)

Push to `dev` (or merge a PR into it) and confirm in the Actions run:

1. `unit-tests` runs and passes first (`baseline` `needs: unit-tests`).
2. `baseline` job runs, uploads an artifact named `sonar-baseline-<full-sha>`
   (visible under the run's Artifacts section).

## 5. `sonar-pr.yml`'s three baseline-resolution tiers

### Tier a - merge-base artifact exists (the common case)

Open a PR against `dev` from a branch forked off `dev`'s current HEAD (which
already has a `sonar-baseline-<sha>` artifact from section 4). Expect the
step summary and PR comment to say `tier a: artifact for merge-base <sha>`.

### Tier b - target branch's current HEAD artifact exists, merge-base's doesn't

Needs two divergent points on `dev` with only the newer one still having an
artifact - not something that happens naturally on a low-traffic branch, so
set it up deliberately:

1. Note `dev`'s current HEAD (`OLD`).
2. Branch a test PR off `OLD`.
3. Push a new commit to `dev` directly (or merge something else), producing
   a new HEAD (`NEW`) with its own fresh `sonar-baseline-<NEW>` artifact.
4. Delete the artifact for `OLD` (see section 8 for how).
5. Open/refresh the PR from step 2 (still forked from `OLD`). Its
   merge-base is `OLD` (no artifact), but the target's current HEAD is
   `NEW` (has one) - tier b should fire.

Expect: `tier b: artifact for target branch HEAD <NEW sha>`.

### Fallback - neither artifact exists

Delete (see section 8) any `sonar-baseline-*` artifacts for both the PR's
merge-base and the target branch's current HEAD, then open/refresh a PR.
Expect: `fallback: no artifact found, scanning target branch HEAD live in
this job`.

This is also what happens automatically the first time a PR is ever opened
against a repo (no baseline artifact has been published yet), or whenever
`project-base-dir` doesn't exist yet on the target branch (see
`scripts/find_baseline_artifact.py` and `sonar-pr.yml`'s "Check whether
target branch has project-base-dir" step) - no manual artifact deletion
needed to exercise those specific cases.

## 6. Hash-based matching survives line churn

In a test PR, change a file under `project-base-dir` two ways at once:

- Insert a blank/comment line **earlier** in the file than an existing
  finding, shifting that finding's line number without changing its content.
- Introduce one genuinely new violation elsewhere in the file.

Expect `diff_findings.py`'s output (and the PR's new-findings count) to
contain exactly the new violation - the shifted one must not appear, since
it's matched by `hash`, not `line`. (`fixtures/python/app.py` and
`fixtures/js/app.js` are deliberately kept minimal specifically so this kind
of edit is easy to make by hand when testing.)

## 7. Blocking policy

Introduce a change that trips a MAJOR-or-above rule (or reuse the existing
findings already in `fixtures/`, which trip 5 MAJOR-or-above findings by
design, including one Security Hotspot) in a test PR. Expect:

- `pr-scan` job conclusion: `failure`
- Check Run `sonarqube-community/new-findings`: conclusion `failure`
- PR comment: `**Blocked** - a new finding meets or exceeds the ... threshold.`

Revert the change (or lower `severity-threshold` / set `blocking: false` as
inputs on `sonar-pr.yml`) and confirm the same PR goes green without
re-triggering a code review - the gate should only care about current state.

## 8. Deleting a baseline artifact

Needed for sections 4 (tier b, fallback) and for general cleanup after
testing. Either:

- GitHub UI: the workflow run's page → Artifacts section → the artifact's
  "…" menu → Delete.
- API (needs a token with artifact-delete permission - a workflow's own
  `GITHUB_TOKEN` does not have it):
  ```sh
  curl -X DELETE -H "Authorization: Bearer $TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/<owner>/<repo>/actions/artifacts/<artifact_id>"
  ```
  Find `<artifact_id>` via
  `GET /repos/<owner>/<repo>/actions/artifacts?name=sonar-baseline-<sha>`.

## 9. Cleanup after manual testing

- Close/delete scratch branches and PRs created for this procedure.
- Delete any `sonar-baseline-*` (and `*-fallback-base`, if a fallback scan
  ran under a distinct project key) artifacts created purely for testing,
  so they don't linger and skew a later real PR's baseline resolution.
- Revert any deliberate `fixtures/` changes made for section 6 or 7 that
  weren't meant to be permanent.
