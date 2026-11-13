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

**`scanner-mode: dotnet` and the fallback scan.** The fallback path builds
the target branch too (same `build-command`), so it can fail on code the PR
never touched - stale history, a solution added after that commit, a flaky
restore. If the failure really is that build (`scripts/baseline_fallback.py`
checks the action's `failure-reason` output against a fixed set of
build-related reasons), `sonar-pr.yml` uses an empty baseline instead -
every PR head finding is reported as new - and says so in the job summary,
the Check Run, and the PR comment, via the same baseline-tier text the
artifact tiers above already use. A failure for any OTHER reason (the
ephemeral SonarQube instance itself down, a bad scanner install, ...) is
never reinterpreted that way - it fails the job exactly as before this
existed, so an infrastructure problem can't hide behind what looks like a
merely permissive baseline. The PR head's own scan is unaffected either
way: a real head build failure still fails the job exactly as today.

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
| `nuget-auth-token` | `""` | Optional. A credential for a private NuGet feed `build-command` needs to restore from. Exposed as `NUGET_AUTH_TOKEN` to the "Run SonarQube scan" step (and so to `build-command`) only - never to `begin`/`end` or the fetch/report steps after it. On `sonar-pr.yml`/`sonar-baseline.yml` this is a `secrets:` input, not a plain `with:` one (see below). |

On the reusable workflows, `timeout-minutes` (default `30`, unchanged) may
need raising in `dotnet` mode: the PR workflow's fallback path is a second
restore + build + scan on top of the head scan's, in one job.

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

  "Compiled" means the file appears in the scanner's own records: the
  project's `FilesToAnalyze` list, or the Sonar analyzer's
  `file-metadata.pb`, which lists every file in the compilation. So
  `<SonarQubeExclude>` counts as compiled, both for a whole project and for
  a single file (`<Compile Update="X.cs"><SonarQubeExclude>true</SonarQubeExclude></Compile>`).
  A file kept on disk but removed from the build with
  `<Compile Remove="X.cs" />` really isn't compiled, so it is reported. To
  silence that, list its path in `.sastignore` or in `.sastrc`
  `exclusions.paths`.
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
  `workflow_dispatch`, never automatically on `pull_request`. `sonar-pr.yml`
  enforces this itself (see "Fork PRs in `scanner-mode: dotnet`" below) -
  it's not left to the caller.
- On self-hosted runners, the caller's fork-PR guard on `runner-type`
  matters even more, because the PR's build runs on your machine.
- Check out with `persist-credentials: false`, so the build can't read a
  `GITHUB_TOKEN` from `.git/config`.
- `sonar-pr.yml` clears each fixed checkout subdirectory it reuses
  (`.sast-sonarqube`, `.baseline-checkout`) right before checking it out
  again, and routes every intermediate file a trusted step writes
  (`changed-files.txt`, `new-findings.json`, `policy-result.json`, the
  extracted baseline artifact, ...) through `$RUNNER_TEMP` instead of a
  bare workspace-root filename. `scripts/check_checkout_order.py`
  enforces the checkout-clearing pattern structurally and flags shell
  redirects, `tee`, and output flags in `run:` blocks that bypass
  `$RUNNER_TEMP`; other write forms need manual review.

#### Fork PRs in `scanner-mode: dotnet`

`sonar-pr.yml` gates this itself, before checking out or building anything:
on a `pull_request` event, `scanner-mode: dotnet`, and a head repository
that isn't the base repository, the job fails immediately with a message
explaining that a maintainer must run the workflow for this PR, and the job
summary names the exact `workflow_dispatch` input to use - it never reaches
`actions/checkout` for the PR's code, let alone `build-command`.
`scanner-mode: cli` is unaffected (it never executes PR code, so the gate
doesn't apply to it - a fork PR still gets a full `cli`-mode scan
automatically, same as today).

To scan that PR anyway, a maintainer first reviews the PR's actual head
commit, then dispatches the SAME workflow manually with the PR number AND
the exact SHA of the commit just reviewed, via `pr-number` and `head-sha`
inputs the caller wires through from its own `workflow_dispatch` trigger:

```yaml
# consumer repo: .github/workflows/sonarqube.yml
on:
  pull_request:
    types: [opened, synchronize, reopened]
  workflow_dispatch:
    inputs:
      pr-number:
        description: PR number to dispatch a dotnet-mode scan for (fork PRs only)
        required: false
        type: string
      head-sha:
        description: Full commit SHA you reviewed on that PR (required alongside pr-number)
        required: false
        type: string

jobs:
  pr-scan:
    if: github.event_name == 'pull_request' || github.event_name == 'workflow_dispatch'
    uses: vln-devsecops/actions-sast-sonarqube/.github/workflows/sonar-pr.yml@v1
    permissions:
      contents: read
      checks: write
      pull-requests: write
      actions: read
    with:
      scanner-mode: dotnet
      build-command: dotnet build MySolution.sln
      pr-number: ${{ github.event.inputs.pr-number }}
      head-sha: ${{ github.event.inputs.head-sha }}
```

`head-sha` is not optional in practice, even though the input itself has
no `required: true` (that would force every `pull_request`-triggered caller
to also pass it on every ordinary run): dispatching with a PR number alone
would be a TOCTOU hole - a fork can push a new, unreviewed commit after the
maintainer's review but before (or while) the dispatched run starts, and
that new, unreviewed commit is what would actually get built with this
job's write-scoped tokens, not the one anyone looked at. It could also
tamper with `.sast-sonarqube/` scripts or the baseline JSON that same job
later trusts, to green its own check. So before checking out or building
anything, the dispatched run re-fetches the PR's *live* state from the
GitHub API (`scripts/resolve_pr_context.py`'s `verify_dispatch_pr_context()`)
and refuses - with a message naming exactly what changed - unless:

- the PR is still `open` (not closed or merged),
- its live head SHA still equals `head-sha` exactly, and
- the API actually returned a head SHA, base SHA, and base ref (an
  incomplete response is refused rather than trusted).

Only once all three hold does it check out and scan exactly that commit,
and post the named Check Run (`check-name` input) on that exact head SHA -
so a branch rule requiring that check only goes green after a real scan of
reviewed code, not merely because someone clicked a button. Unlike the
automatic `pull_request` case, a `workflow_dispatch` run is NOT subject to
GitHub's read-only `GITHUB_TOKEN` downgrade (that downgrade is specific to
the `pull_request` event from a fork, not to `workflow_dispatch` - see
[events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)),
so it also posts the PR comment if `post-pr-comment` is enabled. Dispatching
a workflow itself needs write access to the repository (see
[manually running a workflow](https://docs.github.com/en/actions/using-workflows/manually-running-a-workflow)),
which is exactly the "maintainer" boundary this gate relies on.

**Branch protection must require the right check.** Set the required status
check to the named Check Run (`check-name`, e.g.
`sonarqube-community/new-findings-dotnet`) with its source set to the
**GitHub Actions** app - not "the job" (e.g. `pr-scan-dotnet / pr-scan`).
Why this distinction matters here specifically: a `pull_request` from a
fork runs the PR's OWN copy of the caller's workflow file, so its author
can edit that file to make the JOB itself report green (skip steps, exit 0
early, whatever) - but that fork PR's read-only `GITHUB_TOKEN` (see above)
means it can never actually POST the named Check Run, since publishing one
needs `checks: write`. Requiring the Check Run, not the job, is what makes
editing the workflow file pointless as a bypass. One more consequence
worth knowing: the fork PR's own automatic job - the one this gate blocked
- stays red even AFTER a maintainer successfully dispatches and the Check
Run goes green; that's expected, not a sign the dispatch didn't work. A
branch rule that requires the Check Run by name merges the PR once that
named check is green, regardless of which run (automatic or dispatched)
posted it.

A private NuGet feed credential, when `build-command` needs one, is wired
as a `secrets:` input on both reusable workflows - not a plain `with:`
one, since it's a secret:

```yaml
    with:
      scanner-mode: dotnet
      build-command: dotnet build MySolution.sln
    secrets:
      nuget-auth-token: ${{ secrets.MY_PRIVATE_FEED_TOKEN }}
      # or: secrets: inherit
```

A `pull_request` from a fork gets no secrets at all except a read-only
`GITHUB_TOKEN` (GitHub's own behavior, not this action's) - moot for the
automatic case here, since the fork-PR gate above blocks it before any
build runs, but still true for a maintainer-dispatched scan of a fork PR
whose `build-command` restores from a private feed: wire the secret through
the SAME way regardless, and a missing one just means that restore fails
loudly (the action already fails the job on any `build-command` failure)
rather than silently scanning nothing.

#### Reusable-workflow event context

`sonar-pr.yml`'s fork-PR gate (and its "Post PR summary comment" step, which
predates it) read `github.event_name` and `github.event.pull_request.*`
directly. Inside a workflow invoked via `workflow_call`, these are the
CALLER's (e.g. `"pull_request"`), never `"workflow_call"` itself - GitHub's
own docs state it directly: "When a reusable workflow is triggered by a
caller workflow, the `github` context is always associated with the caller
workflow" -
[Reusing workflow configurations](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations).
This is what this file already relied on, live, before the fork-PR gate
existed. What a reusable workflow genuinely can't resolve on a caller's
behalf is *which runner pool* to use (see the `runner-type` input's own
description) - that's caller-specific policy, not something event data
can answer.

**`pull_request_target` is NOT supported, in any `scanner-mode`** -
`resolve_pr_context.py`'s `decide()` returns an error for it outright,
rather than treating it as an alias of `pull_request` (GitHub documents
`pull_request_target` as using the identical `pull_request` webhook
payload - same `github.event.pull_request` object, same fork/base `repo`
fields - see [events that trigger workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows),
so an alias would have been easy to make work). The reason is specific to
what that trigger grants: it runs with the BASE repository's own token
and secrets against a fork's PR-controlled content -
including `.sastrc`/`.sastignore` (an `.sastignore` of `**` suppresses
every finding). Even in `cli` mode, where the automatic fork-PR gate above
never applies at all (the whole premise is that `cli` mode is always safe
to run automatically), a fork author under `pull_request_target` could get
a green REQUIRED Check Run posted with a write-scoped token and no review
in the loop - `cli` mode's safety argument doesn't cover this trigger the
way it covers `pull_request`. Nothing at this project's pre-`dotnet`-mode
baseline documented or supported `pull_request_target`, and no known
caller uses it, so rejecting it outright is the conservative default;
supporting it would need its own deliberate design, not a side effect of
one event payload happening to look like another's.

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

For a `.NET` repo (`scanner-mode: dotnet`), including the fork-PR
`workflow_dispatch` escape hatch and an optional private NuGet feed
credential, see "Fork PRs in `scanner-mode: dotnet`" above - the `on:`/
`with:`/`secrets:` wiring differs enough from the plain-CLI example above
that it's shown there, next to the trust note it depends on, rather than
duplicated here.

**Fork PRs, `scanner-mode: cli`** (default): GitHub downgrades
`GITHUB_TOKEN` to read-only for a `pull_request` event from a fork,
regardless of the `permissions:` block above. `sonar-pr.yml` still runs the
full scan and diff, and still blocks the job on policy the same as any
other PR - it just can't publish the Check Run or PR comment, since both
need write access it doesn't have. Findings are reported in the job's step
summary instead in that case.

**Fork PRs, `scanner-mode: dotnet`**: NOT scanned automatically at all -
see "Fork PRs in `scanner-mode: dotnet`" above. `cli` mode's read-only-token
behavior above doesn't apply here because the job never gets that far.

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
