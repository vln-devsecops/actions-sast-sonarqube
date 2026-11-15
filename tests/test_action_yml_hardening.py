"""Structural checks on action.yml itself.

Two invariants this pins:

* no `run:` block interpolates a `${{ ... }}` template expression directly
  into the shell - every input/step value a `run:` block needs crosses into
  the shell as a quoted `env:` variable instead. A `${{ }}` expression is
  substituted into the step's script text *before* the shell ever sees it,
  so any input that can contain shell metacharacters (`"`, `` ` ``, `$(...)`)
  is interpreted as script, not data - and a bare `${{ inputs.sonar-token }}`
  also puts the token's value in the rendered script GitHub logs if that
  step's output is ever echoed or the step fails verbosely upstream.
* no step passes a token on its command line (`--token`, `sonar.token`,
  `sonar.login`) - a command-line argument is visible in that process's argv
  to any other process on the same host (e.g. via /proc/*/cmdline), unlike
  an environment variable scoped to the step.
"""
import re
from pathlib import Path

import yaml

ACTION_YML = Path(__file__).resolve().parent.parent / "action.yml"

TOKEN_ON_COMMAND_LINE = re.compile(r"--token\b|sonar\.token\b|sonar\.login\b")


def _steps():
    data = yaml.safe_load(ACTION_YML.read_text())
    return data["runs"]["steps"]


def test_no_run_block_contains_a_template_expression():
    offenders = [step.get("name") for step in _steps() if "${{" in str(step.get("run", ""))]
    assert offenders == [], f"run: blocks still interpolate `\\${{{{ }}}}`: {offenders}"


def test_no_step_passes_a_token_on_the_command_line():
    offenders = [
        step.get("name") for step in _steps() if TOKEN_ON_COMMAND_LINE.search(str(step.get("run", "")))
    ]
    assert offenders == [], f"steps pass a token via the command line: {offenders}"
