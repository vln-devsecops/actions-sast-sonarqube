#!/usr/bin/env python3
"""Shared GitHub REST client for this action's scripts.

Consolidates what used to be three near-identical copies in apply_policy.py,
post_pr_comment.py and find_baseline_artifact.py. Keeping one copy matters
beyond DRY: the cross-host redirect handling below was originally fixed in
only one of the three, leaving the others latently broken against the exact
same failure (see StripAuthOnRedirect).
"""
import json
import sys
import time
import urllib.error
import urllib.request

API_VERSION = "2022-11-28"
RETRYABLE_STATUS = (500, 502, 503, 504)
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 2


class StripAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """Artifact `archive_download_url`s (and similar) 302 to a signed,
    short-lived blob-storage URL that doesn't expect (and errors on) our
    GitHub Authorization header - urllib forwards all original headers
    across a redirect by default, which breaks the download with a 401 from
    the storage host, not from GitHub."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is not None:
            new_req.remove_header("Authorization")
        return new_req


def _build_request(method, url, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", API_VERSION)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    return req


def is_rate_limited(headers):
    """A 403 with the rate-limit budget exhausted is a very different
    problem from a 403 for missing permissions; callers should say so."""
    return headers is not None and headers.get("x-ratelimit-remaining") == "0"


def request(method, url, token, body=None, timeout=30, raw=False, allow_forbidden=False):  # pragma: no cover - network I/O, validated live
    """Perform a GitHub API request, retrying transient server errors.

    Returns parsed JSON, or raw bytes when raw=True. Raises SystemExit with a
    readable message on a non-retryable failure.

    allow_forbidden=True treats a 403/404 as "couldn't complete this call,
    not a genuine error" and returns None instead of raising - GitHub
    downgrades GITHUB_TOKEN to read-only for a pull_request event from a
    fork, regardless of the permissions: block a workflow requests, so a
    write call 403ing there is expected, not a misconfiguration.
    """
    opener = urllib.request.build_opener(StripAuthOnRedirect)
    last_error = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = _build_request(method, url, token, body)
        try:
            with opener.open(req, timeout=timeout) as resp:
                return resp.read() if raw else json.load(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if allow_forbidden and e.code in (403, 404):
                print(
                    f"warning: {method} {url} returned HTTP {e.code}; this is expected for a "
                    f"pull request from a fork, where GITHUB_TOKEN is read-only. Findings are "
                    f"still reported in the job step summary.",
                    file=sys.stderr,
                )
                return None
            if e.code == 403 and is_rate_limited(e.headers):
                raise SystemExit(
                    f"GitHub API {method} {url} failed: rate limit exhausted "
                    f"(HTTP 403).\n{detail}"
                )
            if e.code not in RETRYABLE_STATUS:
                raise SystemExit(f"GitHub API {method} {url} failed: HTTP {e.code}\n{detail}")
            last_error = f"HTTP {e.code}\n{detail}"
        except urllib.error.URLError as e:
            # DNS/TLS/connection failures used to escape the old per-script
            # helpers entirely, surfacing as a raw traceback instead of the
            # readable SystemExit message every other failure mode gets.
            last_error = f"{type(e).__name__}: {e.reason}"

        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF_SECONDS * attempt)

    raise SystemExit(f"GitHub API {method} {url} failed after {MAX_ATTEMPTS} attempts: {last_error}")
