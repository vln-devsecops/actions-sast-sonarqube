import pytest

from resolve_pr_context import ACTION_BLOCK, ACTION_DISPATCH, ACTION_ERROR, ACTION_SCAN, decide, fetch_pr_context


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
    result = decide("pull_request", event, "dotnet", "")
    assert result["action"] == ACTION_SCAN
    assert result["is_fork"] is False
    assert result["pr_number"] == 42
    assert result["head_sha"] == "headsha"
    assert result["base_sha"] == "basesha"
    assert result["base_ref"] == "main"


def test_same_repo_pr_in_cli_mode_scans():
    event = pull_request_event(head_repo="acme/app", base_repo="acme/app")
    result = decide("pull_request", event, "cli", "")
    assert result["action"] == ACTION_SCAN
    assert result["is_fork"] is False


# --- fork PR fixture -----------------------------------------------------------

def test_fork_pr_in_dotnet_mode_blocks():
    event = pull_request_event(head_repo="someone/app-fork", base_repo="acme/app")
    result = decide("pull_request", event, "dotnet", "")
    assert result["action"] == ACTION_BLOCK
    assert result["is_fork"] is True
    assert "pr-number=42" in result["reason"]
    assert "maintainer" in result["reason"]


def test_fork_pr_in_cli_mode_still_scans():
    """cli mode never executes PR code, so the fork gate must not apply to
    it - today's behaviour (scan runs, job summary only) stays unchanged."""
    event = pull_request_event(head_repo="someone/app-fork", base_repo="acme/app")
    result = decide("pull_request", event, "cli", "")
    assert result["action"] == ACTION_SCAN
    assert result["is_fork"] is True


def test_fork_pr_with_deleted_head_repo_is_treated_as_a_fork():
    """GitHub returns head.repo: null when the fork was deleted - must not
    crash, and must not be mistaken for a same-repo PR."""
    event = pull_request_event(head_repo=None, base_repo="acme/app")
    result = decide("pull_request", event, "dotnet", "")
    assert result["action"] == ACTION_BLOCK
    assert result["is_fork"] is True


def test_pull_request_event_missing_pull_request_object_errors():
    result = decide("pull_request", {"number": 1}, "dotnet", "")
    assert result["action"] == ACTION_ERROR


# --- workflow_dispatch with a PR number fixture --------------------------------

def test_dispatch_with_pr_number_is_dispatch_action():
    result = decide("workflow_dispatch", {}, "dotnet", "42")
    assert result["action"] == ACTION_DISPATCH
    assert result["pr_number"] == 42
    assert "42" in result["reason"]


def test_dispatch_with_pr_number_works_in_cli_mode_too():
    result = decide("workflow_dispatch", {}, "cli", "7")
    assert result["action"] == ACTION_DISPATCH
    assert result["pr_number"] == 7


@pytest.mark.parametrize("raw", ["0", "-1"])
def test_dispatch_with_non_positive_pr_number_errors(raw):
    result = decide("workflow_dispatch", {}, "dotnet", raw)
    assert result["action"] == ACTION_ERROR


def test_dispatch_with_non_numeric_pr_number_errors():
    result = decide("workflow_dispatch", {}, "dotnet", "not-a-number")
    assert result["action"] == ACTION_ERROR


# --- workflow_dispatch without a PR number fixture -----------------------------

@pytest.mark.parametrize("raw", ["", "   ", None])
def test_dispatch_without_pr_number_errors(raw):
    result = decide("workflow_dispatch", {}, "dotnet", raw)
    assert result["action"] == ACTION_ERROR
    assert "pr-number" in result["reason"]


# --- push fixture ---------------------------------------------------------------

def test_push_event_scans_unconditionally():
    result = decide("push", {"ref": "refs/heads/main"}, "dotnet", "")
    assert result["action"] == ACTION_SCAN


# --- unsupported event -----------------------------------------------------------

def test_unsupported_event_name_errors():
    result = decide("schedule", {}, "dotnet", "")
    assert result["action"] == ACTION_ERROR


# --- fetch_pr_context (network path, mocked) -------------------------------------

def test_fetch_pr_context_detects_fork(monkeypatch):
    import resolve_pr_context

    def fake_gh_api(method, url, token):
        assert method == "GET"
        assert url == "https://api.github.com/repos/acme/app/pulls/42"
        return {
            "head": {"sha": "headsha", "repo": {"full_name": "someone/app-fork"}},
            "base": {"sha": "basesha", "ref": "main", "repo": {"full_name": "acme/app"}},
        }

    monkeypatch.setattr(resolve_pr_context, "gh_api", fake_gh_api)
    result = fetch_pr_context("acme/app", 42, "token")
    assert result == {"is_fork": True, "head_sha": "headsha", "base_sha": "basesha", "base_ref": "main"}


def test_fetch_pr_context_detects_same_repo(monkeypatch):
    import resolve_pr_context

    def fake_gh_api(method, url, token):
        return {
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
            "head": {"sha": "headsha", "repo": None},
            "base": {"sha": "basesha", "ref": "main", "repo": {"full_name": "acme/app"}},
        }

    monkeypatch.setattr(resolve_pr_context, "gh_api", fake_gh_api)
    result = fetch_pr_context("acme/app", 42, "token")
    assert result["is_fork"] is True
