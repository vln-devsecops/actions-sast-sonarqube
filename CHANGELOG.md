# Changelog

## [1.2.0](https://github.com/vln-devsecops/actions-sast-sonarqube/compare/v1.1.0...v1.2.0) (2026-08-26)


### Features

* Add baseline-scm-history input, disable SCM sensor on shallow baseline scan by default ([#43](https://github.com/vln-devsecops/actions-sast-sonarqube/issues/43)) ([c51c27a](https://github.com/vln-devsecops/actions-sast-sonarqube/commit/c51c27a71c4a3053901e0175e9476fd2525b4f87))


### Bug Fixes

* keep sonar.sources/tests at the project root, carry tests.paths via sonar.test.inclusions ([#47](https://github.com/vln-devsecops/actions-sast-sonarqube/issues/47)) ([3b916fe](https://github.com/vln-devsecops/actions-sast-sonarqube/commit/3b916feec44d6a9473d25e342d9457f0059c1e2f))

## [1.1.0](https://github.com/vln-devsecops/actions-sast-sonarqube/compare/v1.0.2...v1.1.0) (2026-08-22)


### Features

* add .sastrc / .sastignore noise-reduction config files ([a6948e0](https://github.com/vln-devsecops/actions-sast-sonarqube/commit/a6948e06bebe978e04c23a40dad301f9f73e4d54))

## [1.0.2](https://github.com/vln-devsecops/actions-sast-sonarqube/compare/v1.0.1...v1.0.2) (2026-08-20)


### Bug Fixes

* use job.workflow_ref, not the nonexistent github.job_workflow_ref ([5f35969](https://github.com/vln-devsecops/actions-sast-sonarqube/commit/5f35969c6074a00813a85a3bed6894140be3de6c))

## [1.0.1](https://github.com/vln-devsecops/actions-sast-sonarqube/compare/v1.0.0...v1.0.1) (2026-08-20)


### Bug Fixes

* resolve this action's own source via job_workflow_ref, not workflow_ref ([22854e4](https://github.com/vln-devsecops/actions-sast-sonarqube/commit/22854e4d05ca6d31851891b366628291301e9cd3))

## 1.0.0 (2026-08-18)


### Documentation

* pin the consumer example to [@v1](https://github.com/v1) instead of [@main](https://github.com/main) ([3aebddf](https://github.com/vln-devsecops/actions-sast-sonarqube/commit/3aebddf32e638c38ffa7d70fe4715d4b8753400a))
