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
                                findings JSON plus a human-readable Markdown
                                report of every finding's exact location."
                                Used by both workflows below so scan logic
                                lives in exactly one place.

.github/workflows/
  sonar-baseline.yml           Reusable workflow (workflow_call). Scans HEAD
                                of a push to a baseline branch, uploads
                                findings (JSON) and the report (Markdown) as
                                artifact "sonar-baseline-<sha>".

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

The fallback checkout is shallow (`fetch-depth: 1`) and its scan runs with
the SCM sensor disabled (`sonar.scm.disabled=true`), so it doesn't pay for
git blame data it has no use for - that scan only ever gets diffed against
the PR head scan, never browsed for issue authorship. Set
`baseline-scm-history: true` on `sonar-pr.yml` to fetch full history and
enable blame analysis on the fallback scan instead.

`actions/download-artifact` alone can only see artifacts from the *same*
run. Finding "whichever artifact happens to be named
`sonar-baseline-<sha>`" needs a name-filtered listing across the whole repo,
so `scripts/find_baseline_artifact.py` calls the REST API directly
(`GET /repos/{owner}/{repo}/actions/artifacts?name=...`).

### New-finding matching

Two findings across independent analyses are the same issue if they share
`(ruleKey, componentPath, hash)` - or, when `hash` is empty (SonarQube
returns no hash for some issue types, e.g. cross-file duplication, and never
returns one for Security Hotspots), `(ruleKey, componentPath, line)`
instead. `hash` is a checksum of the source
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

### Findings report

The SonarQube instance this action spins up is ephemeral - torn down at the
end of the job - so once it finishes there's no SonarQube UI left to browse
for exact issue locations. `action.yml` renders one alongside the findings
JSON on every scan (`scripts/render_findings_report.py`): a Markdown table
of every finding with its rule, severity, and exact `path:line`, covering
the full current finding set rather than just what changed in one PR (Check
Run annotations already cover new findings inline-on-diff for that).

`sonar-baseline.yml` bundles it into the `sonar-baseline-<sha>` artifact
alongside the JSON. `sonar-pr.yml` uploads the PR head scan's report as its
own artifact and links it from the job's step summary.

### Languages this scan can't analyze

By default (`scanner-mode: cli`) this action runs only `sonar-scanner-cli`
in Docker - the plain Community Build CLI, not a build-integrated scanner
(see `scanner-mode: dotnet` below for C#). That CLI does not analyze C# or
VB.NET (files get indexed, but no rules run against them - the scanner
itself logs a `WARN` line to that effect) and has no plugin at all for C,
C++, Objective-C, or Swift (so it never even indexes or logs about them;
`scripts/detect_unanalyzed_languages.py` instead looks for `.c`, `.cc`,
`.cpp`, `.cxx`, `.c++`, `.m`, `.mm`, and `.swift` files under
`project-base-dir`, honoring `.sastrc` `exclusions.paths` and
`.sastignore`). A repo in one of these languages would otherwise get a
scan that looks clean - zero findings - while actually saying nothing
about those files at all.

`.m` is ambiguous beyond Objective-C - MATLAB and Mercury both use it too
- so a repo containing either can get a false "Objective-C not analyzed"
report. There's no Community Build plugin for either language to cross-check
against, so this is a known, accepted false-positive rather than something
`detect_unanalyzed_languages.py` can resolve by content-sniffing a `.m`
file; exclude such files via `.sastignore` if this comes up.

Whenever this scan detects any of these languages, it says so **everywhere
a clean result could otherwise appear**: a `::warning::` annotation and a
job summary entry from this action itself, a banner at the top of the
findings report, and - from `sonar-pr.yml` - the Check Run summary and the
PR comment. The action also exposes an `unanalyzed-languages` output
(comma-separated list of language names, empty string when none) so a
caller can act on it directly.

By default this only warns - it does not fail the job, so an existing
`@v1` caller with an incidental file in one of these languages doesn't
start failing out of nowhere. Set the `fail-on-unanalyzed-languages` input
to `"true"` (on the action directly, or the equivalent input on either
reusable workflow) to fail instead; the findings JSON and findings report
are still produced first, so callers reading them with `if: always()`
aren't affected by the failure.

### Scanning C#: `scanner-mode: dotnet`

C# is analyzed only by the SonarScanner for .NET, which wraps the build:
`begin`, then your build, then `end`. Set `scanner-mode: dotnet` on the
action to use it instead of the CLI. Inputs:

| Input | Default | Meaning |
|---|---|---|
| `scanner-mode` | `cli` | `cli` (unchanged) or `dotnet`. |
| `build-command` | `""` | Required with `dotnet`, rejected with `cli`. Builds the code, e.g. `dotnet build MySolution.sln`. Runs with `bash -c` from `project-base-dir`, between `begin` and `end`. `SONAR_TOKEN` is removed from its environment, but the build can still read the token (see the trust note). |
| `dotnet-version` | `10.0.x` | Passed to `actions/setup-dotnet`, which runs only in `dotnet` mode. |
| `import-roslyn-issues` | `"false"` | `"true"` also imports the project's own Roslyn analyzer issues (StyleCop, CA*, compiler warnings). By default only Sonar's own C# rules are reported (`sonar.cs.roslyn.ignoreIssues=true`). |

```yaml
# a .NET repo, calling the action directly (SonarQube already up - see
# ci.yml's dotnet-e2e job for the stack setup steps)
- uses: vln-devsecops/actions-sast-sonarqube@v1
  with:
    sonar-token: ${{ steps.bootstrap.outputs.token }}
    project-key: my-dotnet-app
    project-base-dir: .
    scanner-mode: dotnet
    dotnet-version: "10.0.x"
    build-command: dotnet build MySolution.sln
```

The scan fails, rather than reporting a clean result, when:

- `build-command` exits non-zero (`end` is not run);
- `begin` or `end` exits non-zero (e.g. nothing was built, or no C# was
  compiled at all);
- any project the build touched was not analyzed, even though `end`
  exited 0 with `ANALYSIS SUCCESSFUL`. That happens when a project fails to
  build behind a build script that swallows the failure, or when projects
  share a duplicate or invalid `ProjectGuid` (`WARNING: Duplicate
  ProjectGuid ... The project will not be analyzed`).
  `scripts/check_dotnet_projects.py` checks this from the scanner's own
  output under `.sonarqube/out/`: every per-project directory must contain
  a `ProjectInfo.xml`, and every project with files to analyze must appear
  in the `sonar.modules` that `end` passed to the scanner engine. A project
  that fails before compilation starts, such as a failed restore, leaves no
  trace there. `build-command`'s exit status catches that case, so don't
  mask build failures. If a build script does mask one, the project's
  `.cs` files are still reported as unanalyzed C# (see below).
  A project excluded on purpose with `<SonarQubeExclude>true</SonarQubeExclude>`
  is not a failure.

Every failure sets the action's `failure-reason` output to a fixed code
(`build-command-failed`, `begin-failed`, `end-failed`,
`no-analyzable-projects`, `project-not-built`, `project-not-analyzed`,
`build-command-required`, `invalid-sast-config`, `no-report-task`, ...)
and writes it to the job summary, so a caller can tell an intended block
from a setup error.

Scope and limits:

- Linux runners and SDK-style projects only. The ephemeral SonarQube
  stack needs Linux and Docker, so Windows-only MSBuild/.NET Framework
  builds are out of scope.
- One `begin`/`end` per invocation. For several solutions, pass a
  `build-command` (or a script) that builds them all.
- `.sastrc` `tests.paths` is **rejected** in `dotnet` mode. The scanner
  ignores `sonar.sources`/`sonar.tests`, and MSBuild decides which projects
  are tests (a test SDK reference, or `<SonarQubeTestProject>`). The other
  `.sastrc`/`.sastignore` settings work as in `cli` mode, with paths
  relative to `project-base-dir`.
- `sonar.projectBaseDir` is set to `project-base-dir`, so `.sastrc` paths
  mean the same in both modes. A project outside `project-base-dir` (for
  example one that `build-command` reaches with `../`) is dropped from the
  analysis. Point `project-base-dir` at a directory that contains every
  project you build.
- This scanner analyzes only what `build-command` compiles. Any `.cs` or
  `.vb` file under `project-base-dir` that no built project compiled is
  reported as unanalyzed C# or VB.NET. That covers a second solution the
  command doesn't build, a project whose restore failed behind `|| true`,
  and a stray file. The report goes through the same `unanalyzed-languages`
  output and `fail-on-unanalyzed-languages` input as above, and only warns
  by default. `bin/`, `obj/`, gitignored files and `.sastrc`/`.sastignore`
  exclusions are skipped. C, C++, Objective-C and Swift files are reported
  as in `cli` mode.
- Coverage import (`sonar.cs.opencover.reportsPaths` and the like) is not
  supported, so `exclusions.coverage_paths` has no effect on C#.
- `dotnet-sonarscanner` is pinned in `.config/dotnet-tools.json`, and
  Dependabot bumps it. It is installed into `RUNNER_TEMP`, and its JRE is
  cached there too (`/d:sonar.userHome`), which keeps it working on a
  self-hosted runner whose `/tmp` is `noexec`.
- The Sonar token is passed as `/d:sonar.token=...` on the `begin` and
  `end` command lines, because the scanner ignores the `SONAR_TOKEN`
  environment variable. GitHub Actions masks it in the log, and the script
  never echoes it. The build can still read it, as the trust note below
  explains. That is an accepted risk: the token is only valid for the
  ephemeral SonarQube server, which dies with the job.

**Trust note.** `cli` mode never runs the code it scans. `dotnet` mode
does: `build-command`, MSBuild targets and NuGet package build scripts all
run on the runner, beside everything the job holds. Build code can rewrite
`.sonarqube/` (including the config `end` reads, so it can redirect `end`
to another server or binary) and, in the same job, the findings files a
policy step later reads. Build code can also read the Sonar token. The
action removes `SONAR_TOKEN` from `build-command`'s environment, but that
is hygiene, not a boundary: the token is still in the parent process's
environment (`/proc/$PPID/environ`) and on `end`'s command line. With it,
build code can, for example, mark its own issues resolved on the
ephemeral server. So `dotnet` mode can't defend the gate against an
untrusted author within one job:

- Run it automatically only for PRs from branches in the same repository
  (trusted authors).
- Run it for PRs from forks only through a maintainer-triggered
  `workflow_dispatch`, never automatically on `pull_request`. The reusable
  workflows' support for this is a separate, upcoming change.
- On self-hosted runners, the caller's fork-PR guard on `runner-type`
  matters even more, because the PR's build runs on your machine.
- Check out with `persist-credentials: false`, so the build can't read a
  `GITHUB_TOKEN` from `.git/config`.

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

Both SonarQube *Issues* (bugs, code smells, vulnerabilities - `api/issues/search`)
and *Security Hotspots* (`api/hotspots/search`) are covered, folded into the
same findings schema and tagged with a `type` field (see
`scripts/fetch_findings.py`). Hotspots have no `severity` of
their own - their `vulnerabilityProbability` (`HIGH`/`MEDIUM`/`LOW`) is
mapped onto the same severity scale issues use (`HIGH`→`CRITICAL`,
`MEDIUM`→`MAJOR`, `LOW`→`MINOR`, a judgment call with no canonical mapping
to fall back on) so the same threshold applies to both.

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
    uses: vln-devsecops/actions-sast-sonarqube/.github/workflows/sonar-baseline.yml@v1
    permissions:
      contents: read
    with:
      baseline-branches: '["main"]'

  pr-scan:
    if: github.event_name == 'pull_request'
    uses: vln-devsecops/actions-sast-sonarqube/.github/workflows/sonar-pr.yml@v1
    permissions:
      contents: read
      checks: write
      pull-requests: write
      actions: read
```

Pin to a version tag, not `@main` or a commit SHA: this repo tags releases
via [release-please](https://github.com/googleapis/release-please). `@v1`
tracks the newest `1.x.y` release (picks up minor and patch updates); `@v1.2`
tracks the newest `1.2.z` patch only; an exact `@v1.2.3` never moves. Prefer
`@v1` unless you need tighter pinning.

**Fork PRs**: GitHub downgrades `GITHUB_TOKEN` to read-only for a
`pull_request` event from a fork, regardless of the `permissions:` block
above. `sonar-pr.yml` still runs the full scan and diff, and still blocks
the job on policy the same as any other PR - it just can't publish the
Check Run or PR comment, since both need write access it doesn't have.
Findings are reported in the job's step summary instead in that case.

## Reducing noise: `.sastrc` and `.sastignore`

SonarQube's default rule set fires on every file it can parse, with no
per-repo tuning out of the box. Drop either or both of these optional files
into `project-base-dir` (paths configurable via the `sast-config-path` /
`sast-ignore-path` inputs, both forwarded by `sonar-baseline.yml` and
`sonar-pr.yml`) to cut that down. Neither file existing reproduces the
exact scan behavior from before this feature existed.

**`.sastignore`** (default path `.sastignore`) - plain path exclusions,
using the same syntax as `.gitignore`: one pattern per line, `#` comments,
blank lines ignored. Use this for the common case of "just don't scan
these paths" (vendored code, generated files, fixtures, etc.):

```gitignore
# .sastignore
vendor/
**/*.generated.go
/build
```

A leading `!` (negation) is rejected with an error rather than silently
mismatched - `sonar.exclusions` has no per-pattern re-include, so a
negated pattern can't be honored faithfully. Drop it, or express the
exception via `.sastrc`'s `ignore` criteria below.

**`.sastrc`** (default path `.sastrc`, YAML) - structured config for
exclusions, test-source classification, and per-rule issue suppression:

```yaml
# .sastrc
exclusions:
  paths: ["**/vendor/**"]           # merged with .sastignore into sonar.exclusions
  coverage_paths: ["**/mocks/**"]   # -> sonar.coverage.exclusions
  duplication_paths: ["**/testdata/**"]  # -> sonar.cpd.exclusions

tests:
  paths: ["src/**/*.test.ts", "features/**"]  # -> sonar.test.inclusions

ignore:                             # -> sonar.issue.ignore.multicriteria
  - rule: "python:S101"             # suppress one specific rule...
    paths: ["tests/**"]             # ...on matching paths
  - rule: "*"                       # or every rule ("*" wildcard)...
    paths: ["legacy/**"]            # ...on a path you're not ready to clean up yet
```

If `tests.paths` is not set, SonarQube guesses which files are test code from
their filenames - a file is classified as a test if its name starts with
`test`, contains `test.` or `tests.`, or sits under a directory named (or
ending in) `doc`, `docs`, `test`, `tests`, `mock`, or `mocks`. SonarQube
applies a different rule set to files classified as tests than to main code,
so an ordinary layout that mixes naming conventions - e.g. `*.test.ts` next
to `*.steps.ts` for the same suite - can end up with only some of its test
files analyzed as tests. Set `tests.paths` explicitly whenever your test
files don't all match the heuristic, or when non-test files accidentally do.

`tests.paths` entries may be glob patterns (`sonar.test.inclusions` supports
them) or bare directories (expanded to match everything under them). They do
NOT map onto `sonar.tests` directly, on purpose: `sonar.tests` only accepts a
list of directories - it rejects wildcards fatally - and narrowing it below
`sonar.sources` makes SonarQube silently scan *only* that directory, with no
warning and a job that still passes. `sonar.sources` and `sonar.tests` are
instead both kept at the project root, with `sonar.test.inclusions` carrying
`tests.paths` to mark which files under that shared root are tests.

Each `ignore` entry maps onto SonarQube's own
[`sonar.issue.ignore.multicriteria`](https://docs.sonarsource.com/sonarqube-community-build/analyzing-source-code/analysis-parameters/)
mechanism (one rule + one path pattern per criterion; a `paths` list with
multiple entries fans out into one criterion per path). It applies to
*Issues* only - Security Hotspots have no rule-suppression mechanism of
their own and always need reviewing directly.

Both files are validated against an explicit schema/allowlist
(`scripts/build_scan_config.py`) and **fail the job closed** - a typo'd or
unrecognized key exits non-zero with a clear error before the scan runs,
rather than silently doing nothing. There is deliberately no way to set
`sonar.host.url`, `sonar.token`, `sonar.projectKey`, `sonar.projectBaseDir`,
or `sonar.working.directory` through either file.

## Testing

```sh
pip install -r requirements-dev.txt
pytest tests/ -v
```

See [`docs/test-procedure.md`](docs/test-procedure.md) for everything else:
a full local end-to-end run, and how to deliberately exercise each
baseline-resolution tier, hash-based matching, and the blocking policy
against a real PR.

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

The full pipeline (scan → fetch → diff → policy) has run against real PRs,
beyond the local ephemeral-instance run: the fallback baseline-resolution
path, the blocking-job-failure path, and tier a artifact-based baseline
resolution have all run end-to-end.

Artifact retention defaults to 90 days (`artifact-retention-days` input on
`sonar-baseline.yml`) - no reason surfaced to deviate from that.
