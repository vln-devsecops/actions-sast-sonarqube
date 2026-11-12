import pytest

from resolve_pr_context import (
    ACTION_BLOCK,
    ACTION_DISPATCH,
    ACTION_ERROR,
    ACTION_SCAN,
    decide,
    fetch_pr_context,
    verify_dispatch_pr_context,
)

VALID_SHA = "a" * 40
OTHER_SHA = "b" * 40


def pull_request_event(head_repo="acme/app", base_repo="acme/app", number=42, head_sha="headsha", base_sha="basesha", base_ref="main"):
    return {
        "number": number,
        "pull_request": {
            "number": number,
            "head": {"sha": head_sha, "repo": {"full_name": head_repo} if head_repo else None},
            "base": {"sha": base_sha, "ref": base_ref, "repo": {"full_name": base_repo} if base_repo else None},
        },
    }


# --- same-repo PR fixture -----------------------------------------------------

def test_same_repo_pr_in_dotnet_mode_scans():
    event = pull_request_event(head_repo="acme/app", base_repo="acme/app")
    result = decide("pull_request", event, "dotnet", "", "")
    assert result["action"] == ACTION_SCAN
    assert result["is_fork"] is False
    assert result["pr_number"] == 42
    assert result["head_sha"] == "headsha"
    assert result["base_sha"] == "basesha"
    assert result["base_ref"] == "main"


def test_same_repo_pr_in_cli_mode_scans():
    event = pull_request_event(head_repo="acme/app", base_repo="acme/app")
    result = decide("pull_request", event, "cli", "", "")
    assert result["action"] == ACTION_SCAN
    assert result["is_fork"] is False


# --- pull_request_target has the identical payload shape ----------------------

def test_pull_request_target_same_repo_scans_in_dotnet_mode():
    """pull_request_target carries the exact same payload as pull_request
    (GitHub's own docs) - it must be handled by the same code path, not
    error out as an unsupported event (that would be a backward-
    compatibility regression for any caller using pull_request_target)."""
    event = pull_request_event(head_repo="acme/app", base_repo="acme/app")
    result = decide("pull_request_target", event, "dotnet", "", "")
    assert result["action"] == ACTION_SCAN


def test_pull_request_target_fork_in_dotnet_mode_blocks():
    """The gate applies at least as strongly to pull_request_target - that
    trigger is typically chosen specifically to grant elevated, base-repo
    permissions while scanning fork content, which is a reason to keep the
    gate, not skip it."""
    event = pull_request_event(head_repo="someone/app-fork", base_repo="acme/app")
    result = decide("pull_request_target", event, "dotnet", "", "")
    assert result["action"] == ACTION_BLOCK


def test_pull_request_target_fork_in_cli_mode_still_scans():
    event = pull_request_event(head_repo="someone/app-fork", base_repo="acme/app")
    result = decide("pull_request_target", event, "cli", "", "")
    assert result["action"] == ACTION_SCAN


# --- fork PR fixture -----------------------------------------------------------

def test_fork_pr_in_dotnet_mode_blocks():
    event = pull_request_event(head_repo="someone/app-fork", base_repo="acme/app")
    result = decide("pull_request", event, "dotnet", "", "")
    assert result["action"] == ACTION_BLOCK
    assert result["is_fork"] is True
    assert "pr-number=42" in result["reason"]
    assert "maintainer" in result["reason"]
    assert "head-sha" in result["reason"]


def test_fork_pr_in_cli_mode_still_scans():
    """cli mode never executes PR code, so the fork gate must not apply to
    it - today's behaviour (scan runs, job summary only) stays unchanged."""
    event = pull_request_event(head_repo="someone/app-fork", base_repo="acme/app")
    result = decide("pull_request", event, "cli", "", "")
    assert result["action"] == ACTION_SCAN
    assert result["is_fork"] is True


def test_fork_pr_with_deleted_head_repo_is_treated_as_a_fork():
    """GitHub returns head.repo: null when the fork was deleted - must not
    crash, and must not be mistaken for a same-repo PR."""
    event = pull_request_event(head_repo=None, base_repo="acme/app")
    result = decide("pull_request", event, "dotnet", "", "")
    assert result["action"] == ACTION_BLOCK
    assert result["is_fork"] is True


def test_pull_request_event_missing_pull_request_object_errors():
    result = decide("pull_request", {"number": 1}, "dotnet", "", "")
    assert result["action"] == ACTION_ERROR


# --- workflow_dispatch with a PR number and head SHA fixture -------------------

def test_dispatch_with_pr_number_and_head_sha_is_dispatch_action():
    result = decide("workflow_dispatch", {}, "dotnet", "42", VALID_SHA)
    assert result["action"] == ACTION_DISPATCH
    assert result["pr_number"] == 42
    assert result["expected_head_sha"] == VALID_SHA
    assert "42" in result["reason"]
    assert VALID_SHA in result["reason"]


def test_dispatch_with_pr_number_and_head_sha_works_in_cli_mode_too():
    result = decide("workflow_dispatch", {}, "cli", "7", VALID_SHA)
    assert result["action"] == ACTION_DISPATCH
    assert result["pr_number"] == 7


@pytest.mark.parametrize("raw", ["0", "-1", "+7", "01", "1_0", "۱۲۳", "abc", "7x"])
def test_dispatch_with_malformed_pr_number_errors(raw):
    """int() would accept "+7", "01" (as 1, silently dropping the leading
    zero's significance) and non-ASCII digit scripts like Arabic-Indic -
    none of those is a PR number GitHub would ever actually show.
    (Surrounding whitespace, e.g. "7 ", is deliberately trimmed first, not
    tested here as a bad case - decide() already does that for pr-number
    and head-sha both, same as the pre-existing pr-number handling.)"""
    result = decide("workflow_dispatch", {}, "dotnet", raw, VALID_SHA)
    assert result["action"] == ACTION_ERROR


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_dispatch_without_pr_number_errors(raw):
    result = decide("workflow_dispatch", {}, "dotnet", raw, VALID_SHA)
    assert result["action"] == ACTION_ERROR
    assert "pr-number" in result["reason"]


@pytest.mark.parametrize("raw", ["", "   ", None])
def test_dispatch_without_head_sha_errors(raw):
    result = decide("workflow_dispatch", {}, "dotnet", "42", raw)
    assert result["action"] == ACTION_ERROR
    assert "head-sha" in result["reason"]


@pytest.mark.parametrize(
    "raw",
    [
        "a" * 39,  # too short
        "a" * 41,  # too long
        "A" * 40,  # upper-case, not what GitHub's API returns
        "g" * 40,  # not hex
        "main",  # a branch name, not a SHA
    ],
)
def test_dispatch_with_malformed_head_sha_errors(raw):
    result = decide("workflow_dispatch", {}, "dotnet", "42", raw)
    assert result["action"] == ACTION_ERROR
    assert "head-sha" in result["reason"]


# --- push fixture ---------------------------------------------------------------

def test_push_event_scans_unconditionally():
    result = decide("push", {"ref": "refs/heads/main"}, "dotnet", "", "")
    assert result["action"] == ACTION_SCAN


# --- unsupported event -----------------------------------------------------------

def test_unsupported_event_name_errors():
    result = decide("schedule", {}, "dotnet", "", "")
    assert result["action"] == ACTION_ERROR


# --- verify_dispatch_pr_context (pure, no network) -------------------------------

def _pr_context(**overrides):
    base = {"state": "open", "head_sha": VALID_SHA, "base_sha": "basesha", "base_ref": "main"}
    base.update(overrides)
    return base


def test_verify_dispatch_pr_context_allows_matching_open_pr():
    assert verify_dispatch_pr_context(42, VALID_SHA, _pr_context()) is None


def test_verify_dispatch_pr_context_rejects_head_sha_mismatch():
    """The TOCTOU case this whole function exists for: a fork pushed a new
    commit after the maintainer's review but before this dispatch ran."""
    error = verify_dispatch_pr_context(42, VALID_SHA, _pr_context(head_sha=OTHER_SHA))
    assert error is not None
    assert VALID_SHA in error
    assert OTHER_SHA in error
    assert "42" in error


def test_verify_dispatch_pr_context_rejects_closed_pr():
    error = verify_dispatch_pr_context(42, VALID_SHA, _pr_context(state="closed"))
    assert error is not None
    assert "closed" in error


def test_verify_dispatch_pr_context_rejects_merged_pr():
    """GitHub reports a merged PR's `state` as "closed" too (there is no
    separate "merged" state value) - covered by the closed case above, but
    pin it explicitly since "merged" is the scenario that actually matters
    here (dispatching a scan for code already on the target branch)."""
    error = verify_dispatch_pr_context(42, VALID_SHA, _pr_context(state="closed"))
    assert error is not None


@pytest.mark.parametrize("missing", ["head_sha", "base_sha", "base_ref"])
def test_verify_dispatch_pr_context_rejects_missing_fields(missing):
    error = verify_dispatch_pr_context(42, VALID_SHA, _pr_context(**{missing: None}))
    assert error is not None
    assert "incomplete" in error


def test_verify_dispatch_pr_context_missing_fields_checked_before_state():
    """Missing head_sha/base_sha/base_ref means the API response couldn't
    be trusted at all - report that, not a possibly-stale state value."""
    error = verify_dispatch_pr_context(42, VALID_SHA, _pr_context(head_sha=None, state="open"))
    assert "incomplete" in error


# --- fetch_pr_context (network path, mocked) -------------------------------------

def test_fetch_pr_context_detects_fork(monkeypatch):
    import resolve_pr_context

    def fake_gh_api(method, url, token):
        assert method == "GET"
        assert url == "https://api.github.com/repos/acme/app/pulls/42"
        return {
            "state": "open",
            "head": {"sha": "headsha", "repo": {"full_name": "someone/app-fork"}},
            "base": {"sha": "basesha", "ref": "main", "repo": {"full_name": "acme/app"}},
        }

    monkeypatch.setattr(resolve_pr_context, "gh_api", fake_gh_api)
    result = fetch_pr_context("acme/app", 42, "token")
    assert result == {
        "state": "open",
        "is_fork": True,
        "head_sha": "headsha",
        "base_sha": "basesha",
        "base_ref": "main",
    }


def test_fetch_pr_context_detects_same_repo(monkeypatch):
    import resolve_pr_context

    def fake_gh_api(method, url, token):
        return {
            "state": "open",
            "head": {"sha": "headsha", "repo": {"full_name": "acme/app"}},
            "base": {"sha": "basesha", "ref": "main", "repo": {"full_name": "acme/app"}},
        }

    monkeypatch.setattr(resolve_pr_context, "gh_api", fake_gh_api)
    result = fetch_pr_context("acme/app", 42, "token")
    assert result["is_fork"] is False


def test_fetch_pr_context_treats_deleted_fork_repo_as_fork(monkeypatch):
    import resolve_pr_context

    def fake_gh_api(method, url, token):
        return {
            "state": "open",
            "head": {"sha": "headsha", "repo": None},
            "base": {"sha": "basesha", "ref": "main", "repo": {"full_name": "acme/app"}},
        }

    monkeypatch.setattr(resolve_pr_context, "gh_api", fake_gh_api)
    result = fetch_pr_context("acme/app", 42, "token")
    assert result["is_fork"] is True


def test_fetch_pr_context_carries_closed_state(monkeypatch):
    import resolve_pr_context

    def fake_gh_api(method, url, token):
        return {
            "state": "closed",
            "head": {"sha": "headsha", "repo": {"full_name": "acme/app"}},
            "base": {"sha": "basesha", "ref": "main", "repo": {"full_name": "acme/app"}},
        }

    monkeypatch.setattr(resolve_pr_context, "gh_api", fake_gh_api)
    result = fetch_pr_context("acme/app", 42, "token")
    assert result["state"] == "closed"
