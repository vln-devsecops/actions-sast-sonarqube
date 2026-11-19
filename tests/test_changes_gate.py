"""The PR scan skips itself when a pull request changes no code.

A cheap `changes` job on a hosted runner lists the PR's files and decides;
`pr-scan` (which holds the caller's runner-type, often a single-slot
self-hosted runner and a ~5 minute SonarQube start) only runs when at least
one changed file is not on the ignore list. Failing to decide must never skip
a scan: every uncertain path outputs scan=true.
"""
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "sonar-pr.yml"


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _decide_step():
    steps = _workflow()["jobs"]["changes"]["steps"]
    (step,) = [s for s in steps if s.get("id") == "decide"]
    return step


def test_changes_job_runs_on_a_hosted_runner_not_the_callers_runner():
    job = _workflow()["jobs"]["changes"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["outputs"]["scan"] == "${{ steps.decide.outputs.scan }}"


def test_changes_job_needs_only_read_permissions_and_checks_nothing_out():
    job = _workflow()["jobs"]["changes"]
    assert job["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert not [s for s in job["steps"] if "checkout" in str(s.get("uses", ""))]


def test_pr_scan_waits_for_changes_and_runs_only_when_it_says_scan():
    job = _workflow()["jobs"]["pr-scan"]
    assert job["needs"] == "changes"
    cond = job["if"]
    assert "needs.changes.outputs.scan == 'true'" in cond
    # the job-level `if:` replaces the implicit success() check
    assert "!cancelled()" not in cond


def test_decide_step_keeps_expressions_out_of_the_script_text():
    assert "${{" not in _decide_step()["run"]


def test_skip_paths_is_an_input_with_a_default():
    inp = _workflow()[True]["workflow_call"]["inputs"]["skip-paths"]
    assert inp["type"] == "string"
    for pattern in ("*.md", "docs/*", ".github/*", "*.lock", "package-lock.json"):
        assert pattern in inp["default"].split("\n"), pattern


def _run(tmp_path, files, *, event="pull_request", skip=None, api_fails=False):
    """Run the decide step with a fake `gh` that prints `files`."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    gh = bindir / "gh"
    listing = "\n".join(files)
    gh.write_text(
        "#!/usr/bin/env bash\n"
        + ("exit 1\n" if api_fails else f"cat <<'LIST'\n{listing}\nLIST\n")
    )
    gh.chmod(0o755)
    out = tmp_path / "out"
    out.write_text("")
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "EVENT_NAME": event,
        "PR_NUMBER": "7",
        "REPO": "o/r",
        "GH_TOKEN": "x",
        "SKIP_PATHS": skip if skip is not None else _workflow()[True]["workflow_call"]["inputs"]["skip-paths"]["default"],
    }
    script = tmp_path / "decide.sh"
    script.write_text("#!/usr/bin/env bash\n" + _decide_step()["run"])
    proc = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    (line,) = [l for l in out.read_text().splitlines() if l.startswith("scan=")]
    return line.split("=", 1)[1]


@pytest.mark.parametrize(
    "files, expected",
    [
        (["README.md"], "false"),
        (["README.md", "docs/guide/setup.md", "LICENSE"], "false"),
        (["package-lock.json"], "false"),
        (["frontend/package-lock.json", "poetry.lock"], "false"),
        ([".github/workflows/ci.yml", ".github/dependabot.yml"], "false"),
        (["logo.png"], "false"),
        (["src/app.py"], "true"),
        (["README.md", "src/app.py"], "true"),
        (["infra/main.tf"], "true"),
        (["package.json"], "true"),
        (["docs/example.py"], "false"),
        (["Dockerfile"], "true"),
    ],
)
def test_decision(tmp_path, files, expected):
    assert _run(tmp_path, files) == expected


def test_a_rename_out_of_code_still_counts_as_a_code_change(tmp_path):
    assert _run(tmp_path, ["notes.md", "src/old.py"]) == "true"


def test_no_files_listed_scans(tmp_path):
    assert _run(tmp_path, []) == "true"


def test_a_failed_file_listing_scans(tmp_path):
    assert _run(tmp_path, [], api_fails=True) == "true"


@pytest.mark.parametrize("event", ["workflow_dispatch", "push", "schedule"])
def test_events_other_than_pull_request_always_scan(tmp_path, event):
    assert _run(tmp_path, ["README.md"], event=event) == "true"


def test_a_custom_skip_list_replaces_the_default(tmp_path):
    custom = textwrap.dedent("""\
        vendor/*
        """).rstrip("\n")
    assert _run(tmp_path, ["vendor/x.c"], skip=custom) == "false"
    assert _run(tmp_path, ["README.md"], skip=custom) == "true"


def test_an_empty_skip_list_scans_everything(tmp_path):
    assert _run(tmp_path, ["README.md"], skip="") == "true"
