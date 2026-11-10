# C# support plan (living document)

**Status:** Phase 0 not started · **Last updated:** 2026-11-10 · **Owner:** rlc

Update the status table and the change log below as work lands. Do not rewrite history; append.

## Why

The action runs only `sonar-scanner-cli` in Docker (`scripts/run_scan.sh`). Per Sonar's docs and forum, the plain CLI does not analyze C#/VB.NET: files may be indexed but no analysis runs, so a C# repo gets a green scan with zero findings. C# needs the Scanner for .NET (`dotnet sonarscanner begin` -> build -> `end`). An adopter challenged our claim; `guidance/runbooks/sast-sonarqube.md` lists C# as "Supported".

Silent false-negative is the real harm. Phase 1 fixes the claim and fails loud; later phases add real support.

The same silent false-negative applies to every language the Community Build can't analyze through the CLI (VB.NET, C, C++, Swift, ...): the site runbook currently says those repos "just produce no findings". Phase 1's detection covers all of them, not only C#.

## Goals / non-goals

- Goal: a C# repo scanned through this action produces real SonarC# findings, normalized into the same findings JSON, diffed/blocked by the existing PR/baseline workflows.
- Goal: until then, never report a clean scan for a C# repo that wasn't analyzed - not in the job log, the job summary, the Check Run, or the PR comment.
- Goal: the blocking gate stays trustworthy in `dotnet` mode even though that mode executes the code under scan (see Phase 3 "Trust boundary").
- Non-goal: VB.NET, F#, C/C++ (not Community Build). Revisit VB.NET after C# lands.
- Non-goal: auto-detecting how to build arbitrary repos. The caller supplies the build command.
- Non-goal: Windows runners and projects that only build with Windows MSBuild (.NET Framework, non-SDK-style). The ephemeral SonarQube stack needs Linux + Docker (`docker compose`, `sysctl vm.max_map_count`), so `dotnet` mode is Linux runners and SDK-style projects only.
- Non-goal (for now): test coverage import (`sonar.cs.opencover.reportsPaths` etc.). Until it exists, `.sastrc` `exclusions.coverage_paths` has no effect for C#; say so in the docs.

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Spike: prove approach against a real C# fixture | not started |
| 1 | Stop the bleeding: fix docs, warn on unanalyzed languages | not started |
| 2 | Tests first: C# fixture + specs (BDD/TDD) | not started |
| 3 | Implement `dotnet` scan mode in `run_scan.sh` + `action.yml` | not started |
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
- The orchestrating session runs on the hardened self-hosted runner (`noexec` `/tmp`), which has Docker and a .NET SDK - so Phase 0, the end-to-end tests, and the JRE/`SONAR_USER_HOME` check run there directly.
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

## Open questions

- Runner requirements: do all target runners (including self-hosted, hardened `noexec` /tmp ones) allow the .NET SDK install and the scanner's JRE?
- Does Community Build apply any C# rule/feature limits relevant to our blocking policy (`apply_policy.py`)?
- Can the build be isolated from the gate's inputs in one job, or does the trust boundary force a two-job split (Phase 3)?
- Fallback baseline in `dotnet` mode when the base branch doesn't build: fail, or empty baseline with a note (Phase 4)?

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
