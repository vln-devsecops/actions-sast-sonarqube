# C# support plan (living document)

**Status:** Phase 0 done; Phases 1-3 PRs open and green, awaiting review; Phase 4 next · **Last updated:** 2026-11-10 · **Owner:** rlc

Update the status table and the change log below as work lands. Do not rewrite history; append.

## Why

The action runs only `sonar-scanner-cli` in Docker (`scripts/run_scan.sh`). Per Sonar's docs and forum, the plain CLI does not analyze C#/VB.NET: files may be indexed but no analysis runs, so a C# repo gets a green scan with zero findings. C# needs the Scanner for .NET (`dotnet sonarscanner begin` -> build -> `end`). An adopter challenged our claim; `guidance/runbooks/sast-sonarqube.md` lists C# as "Supported".

Silent false-negative is the real harm. Phase 1 fixes the claim and fails loud; later phases add real support.

The same silent false-negative applies to every language the Community Build can't analyze through the CLI (VB.NET, C, C++, Swift, ...): the site runbook currently says those repos "just produce no findings". Phase 1's detection covers all of them, not only C#.

## Goals / non-goals

- Goal: a C# repo scanned through this action produces real SonarC# findings, normalized into the same findings JSON, diffed/blocked by the existing PR/baseline workflows.
- Goal: until then, never report a clean scan for a C# repo that wasn't analyzed - not in the job log, the job summary, the Check Run, or the PR comment.
- Goal: the blocking gate stays trustworthy in `dotnet` mode for trusted authors; fork PRs run it only via a maintainer-triggered `workflow_dispatch` (see Decisions and Phase 3 "Trust boundary").
- Non-goal: VB.NET, F#, C/C++ (not Community Build). Revisit VB.NET after C# lands.
- Non-goal: auto-detecting how to build arbitrary repos. The caller supplies the build command.
- Non-goal: Windows runners and projects that only build with Windows MSBuild (.NET Framework, non-SDK-style). The ephemeral SonarQube stack needs Linux + Docker (`docker compose`, `sysctl vm.max_map_count`), so `dotnet` mode is Linux runners and SDK-style projects only.
- Non-goal (for now): test coverage import (`sonar.cs.opencover.reportsPaths` etc.). Until it exists, `.sastrc` `exclusions.coverage_paths` has no effect for C#; say so in the docs.

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Spike: prove approach against a real C# fixture | done; decisions approved 2026-11-11 |
| 1 | Stop the bleeding: fix docs, warn on unanalyzed languages | PRs open: guidance#35, devsecops.vlinder.ca#136, #52 |
| 2 | Tests first: C# fixture + specs (BDD/TDD) | PR #53 open, CI green |
| 3 | Implement `dotnet` scan mode in `run_scan.sh` + `action.yml` | PR #53 open, CI green |
| 4 | Workflows (`sonar-pr.yml`, `sonar-baseline.yml`) + normalization | not started |
| 5 | Docs, runbook, adopter validation, release | not started |

## Roles and orchestration

Agents run the phases; rlc approves. No agent merges, publishes, or contacts the adopter.

**Orchestrator** - one long-lived session for the whole plan.
- Owns this document: the status table, Decisions, Open questions, and change log. Updates them as work lands (append, don't rewrite).
- Advances a phase only when its exit condition is met and rlc has approved the gate below.
- Spawns one implementer per phase (or per PR within a phase) and a reviewer per PR. Does not write implementation code itself.
- Relays reviewer findings back to the implementer; a PR goes to rlc only after the reviewer has no unresolved blocking findings.

**Implementer** - one per phase/PR, in its own worktree or session.
- Tests first (BDD/TDD), then implementation; shows the bad case blocked, not just a green run.
- Opens a PR with a Conventional Commits title, against `dev` (and against `dev` only for `devsecops.vlinder.ca`).
- Phase 0's implementer produces evidence, not code: exact log lines, the `report-task.txt` path, issue counts from `/api/issues/search`, timings, and the doc URLs each flag/property name was checked against. Results go into Decisions via the orchestrator.

**Reviewer** - a fresh session per PR, never the one that wrote it.
- Checks the diff against the phase checklist, the Phase 3 "Trust boundary" mitigations, and backward compatibility of `cli` mode.
- Re-runs the bad-case tests itself rather than trusting CI output, and re-verifies exact Sonar flag/property names against current docs.
- Reports findings (blocking / should-fix / nice-to-have) to the orchestrator; does not push fixes or merge.

**rlc (human gates)** - the orchestrator stops and asks at each of these:
- Approving Phase 0's decisions before Phases 2-4 start.
- Every merge, in every repo.
- Any PR to `vln-devsecops/guidance` or `vln-devsecops/devsecops.vlinder.ca`.
- Changing a default (e.g. `fail-on-unanalyzed-languages`) or anything that implies a major version.
- The reply to the adopter.

**Sequencing**
- Phase 0 and Phase 1 can run in parallel (Phase 1 doesn't depend on the .NET scanner).
- Phases 2-4 wait on Phase 0's approved decisions; within them, Phase 2 tests land before (or in the same PR as, failing first) the Phase 3/4 code they cover.
- Phase 5's C# runbook waits until Phase 3 is proven live; the site PR follows the guidance PR.

**Models**

| Role | Model | Why |
|---|---|---|
| Orchestrator | Opus | Gatekeeping, phase sequencing, deciding when to escalate to rlc; low output volume. |
| Reviewers (all PRs) | Opus | The reviewer is the safety control - trust boundary, fail-closed paths, exact Sonar names. |
| Phase 0 implementer (spike) | Opus | Open-ended investigation whose findings become Decisions. |
| Phase 3 implementer | Opus | `dotnet` mode, token handling, trust-boundary job split. |
| Phase 1, 2, 4, 5 implementers | Sonnet | Well-specified checklists with existing patterns to follow; caught by an Opus reviewer. |

**Prerequisites before starting**
- The orchestrating session runs on the hardened self-hosted runner (`noexec` `/tmp`), which has Docker - so Phase 0, the end-to-end tests, and the JRE/`SONAR_USER_HOME` check run there directly. It has no .NET SDK or Java on the host (checked 2026-11-10); the spike uses the official .NET SDK container instead of installing on the host.
- Local clones of `vln-devsecops/actions-sast-sonarqube`, `vln-devsecops/guidance`, `vln-devsecops/devsecops.vlinder.ca`, and `vln-polaris/fhcs-simulator` are available on the runner.
- GitHub access (push branches, open PRs) for each repo that gets a PR: `actions-sast-sonarqube`, `guidance`, and `devsecops.vlinder.ca`. Add any missing repo to the session before the phase that needs it. `fhcs-simulator` (and the adopter's repo) are read-only for Phase 0; Phase 5's live validation runs the workflow there, which needs a PR adding it - get rlc's approval and access first.

## Phase 0 - Spike (answers the open questions)

Do this by hand against a small public .NET repo and `fhcs-simulator` (`repos/vln-polaris/fhcs-simulator`, a real C# adopter candidate), before designing anything.

- [ ] Confirm the plain CLI result on C#: files indexed, zero C# issues. Check whether 26.8 still logs `Your project contains C# files which cannot be analyzed with the scanner you are using` ([Sonar community](https://community.sonarsource.com/t/warn-your-project-contains-c-files-which-cannot-be-analyzed-with-the-scanner-you-are-using-to-ana/121153)); if so, Phase 1 detects from that log line rather than re-implementing Sonar's exclusion globs. Record the exact lines.
- [ ] Run `dotnet sonarscanner begin/build/end` against the ephemeral SonarQube Community Build 26.8 and confirm SonarC# issues appear via `/api/issues/search`.
- [ ] Decide where the .NET scanner runs: (a) on the runner (needs .NET SDK; setup-dotnet), or (b) in a container image. The CLI step is containerized for isolation; the .NET scanner must run *beside the build*, so (a) is likely forced. Record the decision.
- [ ] Record where the .NET scanner writes `report-task.txt` (expected `.sonarqube/out/.sonar/report-task.txt` under the directory `begin` runs in - unverified). `run_scan.sh` currently reads `.scannerwork/report-task.txt` and relies on `-Dsonar.working.directory`, which the .NET scanner controls itself.
- [ ] Property mapping: which of `build_scan_config.py`'s outputs the .NET scanner accepts via `/d:`. Known from the docs: `sonar.sources` and `sonar.tests` are not supported ([SonarScanner for .NET](https://docs.sonarsource.com/sonarqube-community-build/analyzing-source-code/scanners/dotnet/using/)), so `.sastrc` `tests.paths` (which emits `sonar.sources=.`/`sonar.tests=.`/`sonar.test.inclusions`) has no direct equivalent - test projects are decided by MSBuild. Check `sonar.exclusions`, `sonar.cpd.exclusions`, `sonar.issue.ignore.multicriteria.*`, and `sonar.scm.disabled` on a shallow checkout.
- [ ] Token: the docs require `/d:sonar.token=` on both `begin` and `end` and don't mention a `SONAR_TOKEN` env var. Verify against the pinned scanner version whether env is honoured; if not, the token is on the command line (visible in process listings) - log it as an accepted risk scoped by Phase 3 "Trust boundary".
- [ ] Java for the `end` step: runner-provided JRE vs server-provisioned. Whatever caches it must live under `RUNNER_TEMP` (`SONAR_USER_HOME`), for the same `noexec` `/tmp` reason documented in `run_scan.sh`. Try it on a hardened self-hosted runner.
- [ ] Scanner/server compatibility: record the compatible `dotnet-sonarscanner` version range for server 26.8 and pin inside it.
- [ ] Is a non-blocking alternative viable (Roslyn analyzers / `SonarAnalyzer.CSharp` NuGet + SARIF import)? Compare effort and result fidelity; note SARIF import yields no Sonar issue `hash`, so `diff_findings.py` would fall back to line-based matching (noisier PR diffs). Pick one and log why.
- [ ] Build time and CI minutes cost on a representative repo, including the PR workflow's fallback path (two restores + builds + scans in one job).
- [ ] Private NuGet feed restore: what credentials it needs and how they reach the job.
- [ ] Verify exact flag/property/tool names from Sonar's current docs, not memory.

Exit: a decision recorded in "Decisions" below.

### Phase 0 findings (2026-11-10)

Evidence in `/home/rlc/csharp-spike/logs/` (runner-local). SDK image `mcr.microsoft.com/dotnet/sdk:10.0` (10.0.404), `dotnet-sonarscanner` 12.1.0, server 26.8.0.126808.

- Plain CLI: `.cs` files indexed, 0 issues. 26.8 still logs, in the scanner log only (CE task warnings are empty): `WARN  Your project contains C# files which cannot be analyzed with the scanner you are using. To analyze C# or VB.NET, you must use the SonarScanner for .NET 5.x or higher, see https://redirect.sonarsource.com/doc/install-configure-scanner-msbuild.html`
- begin/build/end works: tiny solution 5 issues (`csharpsquid:S1144`, `S1313`, `S2325`, `external_roslyn:CS0169`, `CA1822`); fhcs-simulator 159 issues, 128 of them external Roslyn/StyleCop (`SA1200`=103). Community Build needs scanner 5.14+.
- `report-task.txt`: `<begin dir>/.sonarqube/out/.sonar/report-task.txt`, same `ceTaskId=` format.
- Properties via `/d:`: `sonar.exclusions`, `sonar.cpd.exclusions`, `sonar.issue.ignore.multicriteria.*`, `sonar.scm.disabled`, `sonar.test.inclusions` work. `sonar.sources`/`sonar.tests` are ignored with a warning (exit 0). `sonar.working.directory` makes begin exit 1.
- Token: `SONAR_TOKEN` env is not honoured; token must be in both steps or neither. A `/s:` settings file on begin keeps it off argv; `end` re-reads it, so the file must exist until `end`.
- JRE is provisioned at **begin**, cached under `$HOME/.sonar/cache`; `SONAR_USER_HOME` env does not move it, `/d:sonar.userHome=` does. Cache on the `noexec` `/tmp`: `end` fails (Permission denied).
- Bad cases: whole build fails / no build / zero C# compiled -> `end` exits 1. **Partial build failure -> `end` exits 0, "ANALYSIS SUCCESSFUL", silently missing the broken project.** **Duplicate/invalid `ProjectGuid` in the `.sln` (fhcs-simulator) -> 2 projects skipped with a WARNING, `end` exits 0.**
- Trust boundary: build-time code can rewrite `.sonarqube/conf/SonarQubeAnalysisConfig.xml` (host URL, `JavaExePath`), redirecting `end` and its token or hijacking what `end` executes; the `/s:` token file is readable by the build too. Scoping the token to begin/end does not protect the gate.
- SARIF route: hashes present when paths resolve, but wrong rule keys (`external_Microsoft (R) Visual C# Compiler:S1313`), every issue typed VULNERABILITY, no quality profile. Rejected.
- Timings: fhcs-simulator 43s scanned vs 15s plain build (+~28s). Not covered: private NuGet feeds (fhcs uses none).

## Phase 1 - Stop the bleeding (ship independently)

- [ ] Correct `guidance/runbooks/sast-sonarqube.md` (the source of truth, in `vln-devsecops/guidance`) so C#/VB.NET are "requires Scanner for .NET + build; not covered by this action yet". Check this repo's README for the same claim.
- [ ] Site: PR against `vln-devsecops/devsecops.vlinder.ca` (branch + PR against `dev`, no direct push, don't merge unless asked) updating `_runbooks/sast-sonarqube.md` (line ~54 lists C# as supported) with the same correction. Also update the byte-identical handoff copy `doc/design_handoff_devsecops_runbooks_site/source-docs/sast-sonarqube.md` (and check `DevSecOps Runbooks Site.dc.html`) so the copies don't drift; check `search-index.json` is regenerated if it embeds the text. Land it in step with the guidance PR, not after. While there, replace "the scan won't fail on those repos, it just produces no findings" with the new warning behaviour.
- [ ] Write the detection test first (BDD/TDD), including the bad case: a fixture containing `.cs` files must produce the "not analyzed" status, and one without must not.
- [ ] Detect unanalyzed languages (C#, VB.NET, C/C++, Swift, ...) in `cli` mode. Preferred source: the scanner's own log line (Phase 0); fallback: file-extension scan under `project-base-dir` honouring `.sastignore` exclusions.
- [ ] Surface it everywhere a "clean" result can appear, not just as a `::warning::`: job summary, findings report (`render_findings_report.py`), Check Run text (`apply_policy.py`), and PR comment (`post_pr_comment.py`). Add an action output (e.g. `unanalyzed-languages`) so callers can gate on it.
- [ ] Default is warn (see Decisions). Add input `fail-on-unanalyzed-languages` (default `false`) for callers who want to fail now.
- [ ] Release as `fix:` (non-breaking, v1.x). PR titles Conventional Commits, e.g. `fix(docs): correct C# support claim`, `fix(scan): report languages the CLI scanner cannot analyze`.

## Phase 2 - Tests first

- [ ] `fixtures-dotnet/` (NOT under `fixtures/`: `ci.yml` scans `fixtures/` in `cli` mode, and a C# fixture there would trip Phase 1's detector on every CI run): tiny SDK-style solution with a few deliberate, stable findings (e.g. a known SonarC# rule such as an unused private field or hard-coded credential), a test project, and a clean variant for the PR-diff test. Pin the rule keys expected.
- [ ] Unit specs for new script logic, mirroring `tests/test_build_scan_config.py`: language detection; mode selection; `/d:` argument translation; `dotnet` mode **fails closed** when `.sastrc` sets `tests.paths` (no `sonar.sources`/`sonar.tests` equivalent) instead of silently dropping it; `scm-disabled` maps to `/d:sonar.scm.disabled=true`; `build-command` is never interpolated into a shell script (passed via env).
- [ ] Spec: missing `report-task.txt` at the .NET scanner's location fails the action with a clear message.
- [ ] CI job in `ci.yml` (separate from the existing `fixtures/` jobs) that runs `fixtures-dotnet/` end to end in `dotnet` mode and asserts the expected rule keys are present. Bad cases that must fail the job, not pass with zero findings: (1) a deliberately broken build; (2) `end` failing; (3) a build that compiles zero C# files.
- [ ] Tamper test for the trust boundary (Phase 3): a fixture whose build step tries to (a) read the Sonar token / `GITHUB_TOKEN`, (b) mark its own issues resolved via the SonarQube API, (c) overwrite findings files. The gate must still block.
- [ ] Regression spec: `cli` mode produces an identical scanner invocation to today with no new inputs set.
- [ ] Extend `docs/test-procedure.md` with a C# section.

## Phase 3 - Implementation (shape subject to Phase 0)

Proposed interface, to be confirmed:

- New input `scanner-mode`: `cli` (default, unchanged) | `dotnet`.
- New input `build-command` (required when `scanner-mode: dotnet`), run between `begin` and `end`, from `project-base-dir`. Passed via `env:` and executed with `bash -c "$BUILD_COMMAND"`; never `${{ inputs.build-command }}` inside `run:` (script injection).
- New input `dotnet-version` (passed to `actions/setup-dotnet`).
- `run_scan.sh`: for `dotnet` mode, run begin/build/end on the runner; read `ceTaskId` from the .NET scanner's `report-task.txt` (Phase 0 location); clean `.sonarqube/` before `begin` the way `.scannerwork/` is cleaned today; keep CE-task polling and everything downstream identical so `fetch_findings.py` is untouched if possible.
- `build_scan_config.py`: add a `dotnet` output mode emitting `/d:` tokens, rejecting `tests.paths` (fail closed, consistent with "a config error must not look like noise reduction applied"), and never emitting `sonar.sources`/`sonar.tests`/`sonar.working.directory`.
- `SONAR_USER_HOME` under `RUNNER_TEMP` (JRE + plugin cache), as for the CLI path.
- Pin `dotnet-sonarscanner` in `.config/dotnet-tools.json` and add a `nuget` ecosystem entry to `.github/dependabot.yml` (it currently covers only `docker` and `github-actions`); the same entry keeps `fixtures-dotnet/` packages current.
- Default `cli` behavior must remain byte-for-byte identical (regression test).

### Trust boundary (new in `dotnet` mode)

Today the PR job never executes the code under scan. In `dotnet` mode, `build-command`, MSBuild targets, and NuGet package build scripts run with whatever the job holds: the ephemeral SonarQube admin token, the findings files `apply_policy.py` reads, and - via `actions/checkout`'s default `persist-credentials: true` - a `GITHUB_TOKEN` with `checks: write` / `pull-requests: write`. A malicious PR could resolve its own issues or rewrite findings and pass the gate. Required mitigations:

- [ ] `persist-credentials: false` on every checkout in `sonar-pr.yml` / `sonar-baseline.yml`.
- [ ] No Sonar token in the build step's environment; scope it to `begin`/`end` only (as far as Phase 0's token finding allows).
- [ ] Diff + policy + Check Run/comment run in a separate job that consumes the findings JSON as an artifact fetched directly from SonarQube after `end`, so the build never shares a filesystem with the gate's inputs. If that isn't achievable, document `dotnet` mode as advisory for untrusted authors.
- [ ] Covered by the Phase 2 tamper test.

## Phase 4 - Workflows and normalization

- [ ] `sonar-pr.yml` / `sonar-baseline.yml`: plumb `scanner-mode`, `build-command`, `dotnet-version`; confirm baseline artifacts and hash-based matching (`diff_findings.py`) work for C# issue shapes (path/line/rule key).
- [ ] Fallback baseline path: `sonar-pr.yml` scans `.baseline-checkout/<project-base-dir>` when no artifact exists. In `dotnet` mode that's a second restore + build from a different directory; `build-command` must be relative-path-safe, and the base branch may not build. Decide: fail, or empty baseline with a loud note.
- [ ] Add a `timeout-minutes` input (the job is hard-coded to 30) - two builds + two scans can exceed it.
- [ ] Private feeds: declare optional `secrets:` on both reusable workflows for NuGet feed credentials, and document fork-PR behaviour (no secrets on `pull_request` from forks, so restore from a private feed fails - must fail loud, not scan nothing).
- [ ] Confirm `fetch_findings.py` pagination and report rendering are language-agnostic.
- [ ] Self-hosted runners: `dotnet` mode runs PR code on the runner, so the caller's fork-PR guard on `runner-type` matters more; restate it in the workflow input docs.

## Phase 5 - Docs, validation, release

- [ ] README: new inputs, example workflow for a .NET repo, the trust-boundary note, Linux/SDK-style-only scope, no coverage import, `tests.paths` unsupported in `dotnet` mode.
- [ ] Update the `sast-sonarqube` runbook's eligibility section to the true support matrix (guidance first, site copy already handled in Phase 1), linking to the new C# runbook below.
- [ ] New related runbook, "Scanning C# with SonarQube" (id `sast-sonarqube-csharp`): author in `vln-devsecops/guidance/runbooks/` first (source of truth), then add `_runbooks/sast-sonarqube-csharp.md` to `devsecops.vlinder.ca` in a second branch + PR against `dev` (don't merge unless asked). Follow the existing runbook front matter (`layout: runbook`, `category: Code & Supply-Chain Scanning`, `related: [sast-sonarqube]`, prerequisites, steps, `updated`, `fleshed: true`) and add `sast-sonarqube-csharp` to the `related` list of `sast-sonarqube`. Content: why the plain CLI can't scan C#, prerequisites (.NET SDK version, a working `build-command`, a solution that builds in CI, Linux runner), the workflow using `scanner-mode: dotnet` with `dotnet-version` and `build-command`, monorepo/multi-solution handling, private NuGet feeds, the `build-command` trust note for fork PRs and self-hosted runners, how to read the findings report, troubleshooting (build fails, zero findings, begin/end mismatch, `tests.paths` rejected), and how to confirm C# was really analyzed. Write it only after Phase 3 is proven live so every step is something we ran. Check the site's tests (`test/unit/search.test.ts`, search index, any runbook-count or featured-list specs) still pass, and the rendered page.
- [ ] Validate live on a real adopter repo (candidate: the one that raised the doubt) and on `fhcs-simulator`; quote real findings, not a green run.
- [ ] Conventional-commit PRs; release-please cuts the version (`feat:` -> minor, v1.x). Flipping `fail-on-unanalyzed-languages` to default `true` is a separate `feat!:` -> v2.0.0, with a migration note for `@v1` adopters.
- [ ] Reply to the adopter with the outcome.

## Decisions

- 2026-11-10 - Unanalyzed languages warn by default in v1 (opt-in fail via `fail-on-unanalyzed-languages`); default flips to fail in v2 - failing now would break `@v1` adopters with incidental `.cs` files, and the warning is surfaced in the Check Run/PR comment so a clean result is never shown silently.
- 2026-11-10 - One `begin`/`end` per action invocation; multi-solution repos pass a `build-command` that builds a `.sln`/`.slnx` or a script building several - no per-solution loop in the action.
- 2026-11-10 - `dotnet` mode is Linux runners and SDK-style projects only - the ephemeral SonarQube stack needs Linux + Docker.
- 2026-11-10 - Site corrections leave `doc/design_handoff_devsecops_runbooks_site/` (the `.dc.html` mock and `source-docs/` copies) untouched - the site repo treats the handoff as a frozen spec (`doc/build-plan.md`, `ci_lint_markdown.yml`) and no earlier runbook edit changed it; this supersedes the Phase 1 item asking to update those copies.

- 2026-11-11 - `dotnet` mode trust model (rlc): PRs from branches in the same repo are trusted; PRs from forks run `dotnet` mode only through a maintainer-triggered `workflow_dispatch`, never automatically. Applies to the proposed `dotnet`-mode PR workflow only; `cli` mode is unchanged - build code can rewrite `.sonarqube/conf` and redirect `end`, so the gate can't defend against an untrusted author in one job.
- 2026-11-11 - Import only Sonar's own C# rules by default: `/d:sonar.cs.roslyn.ignoreIssues=true`, with an input to opt back in (rlc) - the repo's own StyleCop/CA analyzers were 128 of fhcs-simulator's 159 issues and are already enforced by its build.

- 2026-11-11 - Pin `dotnet-sonarscanner` 12.1.0 in `.config/dotnet-tools.json`, bumped by Dependabot via a `nuget` entry (rlc) - verified live against 26.8; Community Build needs 5.14+.
- 2026-11-11 - Run begin/build/end on the runner via `actions/setup-dotnet` (`dotnet-version` input) (rlc) - the scanner must share the build's process tree and directory.
- 2026-11-11 - Read `ceTaskId` from `.sonarqube/out/.sonar/report-task.txt` - verified in the spike, same line format as the CLI.
- 2026-11-11 - Pass the token on the command line, `/d:sonar.token` on both begin and end (rlc) - the server is ephemeral and the token is already masked in logs; `SONAR_TOKEN` env is not honoured. Supersedes the plan's "never on the command line".
- 2026-11-11 - Cache the scanner's JRE under `RUNNER_TEMP` via `/d:sonar.userHome` (rlc) - `SONAR_USER_HOME` env doesn't move it, and a `noexec` cache breaks `end`.
- 2026-11-11 - Fail the action on a non-zero `build-command` exit and on any project `end` skipped (`will not be analyzed`, invalid `ProjectGuid`, fewer analyzed projects than expected) (rlc) - `end` exits 0 on partial builds; fhcs-simulator will fail until its `.sln` GUIDs are fixed.
- 2026-11-11 - Phase 1 detects unanalyzed C#/VB.NET from the CLI's `Your project contains ... files which cannot be analyzed` log line - still logged by 26.8, not exposed via CE warnings.
- 2026-11-11 - Reject the SARIF import route (rlc) - wrong rule keys, every issue typed VULNERABILITY, no quality profile.

- 2026-11-12 - Fork PRs in `dotnet` mode (rlc): the fork PR's SAST job fails with "a maintainer must run the SAST workflow for this PR" and skips the build; the maintainer-triggered `workflow_dispatch` run posts the named Check Run on the PR's head SHA, so a branch rule requiring that check only goes green after a real scan.
- 2026-11-12 - Fallback baseline in `dotnet` mode that fails to build (rlc): use an empty baseline (every head finding is new) and say so in the job summary, Check Run and PR comment; the PR head's own scan must still succeed.

## Open questions

- Runner requirements: do all target runners (including self-hosted, hardened `noexec` /tmp ones) allow the .NET SDK install and the scanner's JRE?
- Does Community Build apply any C# rule/feature limits relevant to our blocking policy (`apply_policy.py`)?
- Can the build be isolated from the gate's inputs in one job, or does the trust boundary force a two-job split (Phase 3)?

## Follow-ups (outside this plan's phases)

- Pre-existing: `action.yml`'s fetch and report steps interpolate `${{ inputs.* }}` (project-key, sonar-host-url, and the token on `fetch_findings.py`'s argv) directly inside `run:`. Move them to `env:`. Found in the Phase 2-3 review; present since before this plan; separate `fix(action):` PR.

## Risks

- Build failures masquerading as "no findings". Mitigate: fail the action if `build-command` or `end` fails, or if zero C# files were analyzed; test that explicitly.
- Code under scan tampering with the gate (Phase 3 "Trust boundary").
- Sonar token on the `begin`/`end` command line if the scanner has no env-var option.
- Added CI time/cost, doubled on the PR fallback path; self-hosted runner capacity (see the runner cost pass).
- Scanner/Sonar version skew between .NET scanner and server 26.8.

## Change log

- 2026-11-10 - Plan created after an adopter challenged the "C# without msbuild" claim; confirmed plain CLI does not analyze C#.
- 2026-11-10 - Added `devsecops.vlinder.ca` site updates (Phase 1: correct the existing runbook; Phase 5: a new related C# runbook, not a mirror); site runbooks are downstream copies of `guidance/runbooks`, which stays the source of truth.
- 2026-11-10 - Review pass: added the `dotnet`-mode trust boundary (build runs PR code beside the gate's inputs); `.sastrc` `tests.paths` fails closed under the .NET scanner (no `sonar.sources`/`sonar.tests`); `report-task.txt` location, token, JRE/`SONAR_USER_HOME` spikes; moved the C# fixture out of `fixtures/`; widened Phase 1 detection to all unanalyzed languages and to the Check Run/PR comment; fallback-baseline double build, job timeout, private NuGet feeds; Linux/SDK-style scope; coverage import as non-goal; Dependabot `nuget` pin; recorded warn-now/fail-in-v2, one begin/end per invocation, and Linux-only decisions.
- 2026-11-10 - Added "Roles and orchestration": orchestrator, per-phase implementers, independent per-PR reviewers, rlc's approval gates, sequencing, and access/environment prerequisites.
- 2026-11-10 - Added per-role model choices (Opus for orchestrator, reviewers, Phase 0 and Phase 3; Sonnet for Phases 1, 2, 4, 5); prerequisites updated: the orchestrating session runs on the hardened self-hosted runner with Docker and a .NET SDK.
- 2026-11-10 - Execution started. Runner check: Docker, `vm.max_map_count` and `noexec` `/tmp` present; .NET SDK and Java are NOT installed on the host, so Phase 0 uses the official .NET SDK container. Phase 0 spike and Phase 1 docs corrections started in parallel; docs changes held as local commits pending rlc's approval to push to `guidance` and the site.
- 2026-11-10 - Phase 1 docs: local commits prepared in `guidance` and the site; independent review found no blockers; fixes requested (drop handoff edits, two site wording errors that still implied C# eligibility, softer "planned to warn" wording, `Mon YYYY` dates).
- 2026-11-10 - Phase 0 spike complete; findings recorded under Phase 0 and proposed decisions under Decisions. Contradicts earlier assumptions: JRE provisioned at begin (not end) and `SONAR_USER_HOME` env insufficient; SARIF hashes exist when paths resolve; `end` exits 0 on partial builds and skipped projects; build code can redirect `end` via `.sonarqube/conf`.
- 2026-11-11 - rlc decided the `dotnet`-mode trust model (fork PRs via maintainer `workflow_dispatch` only) and Sonar-rules-only import; approved pushing the Phase 1 docs branches, pushed to `guidance` and `devsecops.vlinder.ca` (no PRs yet).
- 2026-11-11 - rlc approved the Phase 0 decisions (scanner pin, on-runner, token on command line, JRE under RUNNER_TEMP, fail on partial builds, no SARIF). Phases 2-4 unblocked. Phase 1 code implementer stopped mid-fix on a usage limit; to be resumed.
- 2026-11-11 - Confirmed in GitHub Actions (run 43020361585, throwaway branch since deleted): a composite action whose last step fails still sets its outputs from earlier steps, and the job ends failed - the Phase 1 `sonar-pr.yml` design relies on this.
- 2026-11-11 - Phase 1 PRs opened: plan #51, guidance#35, devsecops.vlinder.ca#136, and #52 (detection + `fail-on-unanalyzed-languages`, after two independent review rounds that fixed: Check Run staying green when failing, steps running after cancel, log-line injection, false positives from `.baseline-checkout`/`node_modules`, non-UTF-8 filenames, submodules).
- 2026-11-11 - Phases 2-3 pushed on `feat/dotnet-scan-mode` (tests first, then implementation; based on #52). Independent review: blocking - `SonarQubeExclude` projects failed the scan; should-fix - CI bad cases must assert their reason, token still readable by the build via the parent process environment (docs corrected), `.cs` files outside built projects reported as unanalyzed. Tamper test deferred to Phase 4 (needs the job split).
- 2026-11-12 - Phases 2-3 PR #53 opened (stacked on #52) after three independent review rounds; also fixed: per-file `SonarQubeExclude` counted as compiled (via the analyzer's `file-metadata.pb`, fails safe), distinct `check-failed` / `scanner-failed` reasons. GitHub CI `dotnet-e2e` green: pinned rules found, every bad case failed with its expected `failure-reason`, excluded project passed, stray C# reported. Note: on the self-hosted runner, run pytest with `--basetemp` under `$HOME` (`/tmp` is noexec).
