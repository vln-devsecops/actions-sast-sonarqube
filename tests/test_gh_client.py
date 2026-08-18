import http.server
import json
import threading
import urllib.request

import pytest

import gh_client
from gh_client import StripAuthOnRedirect, is_rate_limited, request


def _local_http_server(status_code, headers=None):
    """A real local HTTP server returning `status_code` for every request,
    used to exercise request()'s actual urllib error handling rather than
    mocking it away."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _respond(self):
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            body = json.dumps({"message": "denied"}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _respond
        do_POST = _respond
        do_PATCH = _respond

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _flaky_then_ok_server(fail_status, fail_times):
    """A real local HTTP server that returns `fail_status` for the first
    `fail_times` requests, then 200 with a JSON body - used to prove
    request() actually retries transient failures rather than merely
    surviving them once."""
    attempts = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            attempts.append(1)
            if len(attempts) <= fail_times:
                self.send_response(fail_status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
            else:
                body = json.dumps({"ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, attempts


def test_is_rate_limited_true_when_remaining_is_zero():
    assert is_rate_limited({"x-ratelimit-remaining": "0"}) is True


def test_is_rate_limited_false_when_remaining_is_nonzero():
    assert is_rate_limited({"x-ratelimit-remaining": "10"}) is False


def test_is_rate_limited_false_when_header_absent():
    assert is_rate_limited({}) is False


def test_is_rate_limited_false_when_headers_is_none():
    assert is_rate_limited(None) is False


def test_request_raises_on_403_by_default():
    """The ordinary case: a 403 is a genuine error and should stop the job,
    not be silently swallowed."""
    server = _local_http_server(403)
    try:
        with pytest.raises(SystemExit):
            request("POST", f"http://127.0.0.1:{server.server_port}/", "token")
    finally:
        server.shutdown()


def test_request_reports_rate_limit_distinctly_from_a_plain_403():
    """A 403 carrying x-ratelimit-remaining: 0 is a different problem from a
    permissions 403 and should say so, rather than looking identical to the
    genuine fork-PR 403 case."""
    server = _local_http_server(403, headers={"x-ratelimit-remaining": "0"})
    try:
        with pytest.raises(SystemExit, match="rate limit exhausted"):
            request("GET", f"http://127.0.0.1:{server.server_port}/", "token")
    finally:
        server.shutdown()


def test_request_returns_none_on_403_when_forbidden_allowed():
    """GitHub downgrades GITHUB_TOKEN to read-only for a pull_request event
    from a fork, regardless of the permissions: block a workflow requests -
    so a write call 403ing there is expected, not a genuine error, and must
    degrade gracefully rather than crash the whole job."""
    server = _local_http_server(403)
    try:
        result = request("POST", f"http://127.0.0.1:{server.server_port}/", "token", allow_forbidden=True)
    finally:
        server.shutdown()
    assert result is None


def test_request_returns_none_on_404_when_forbidden_allowed():
    server = _local_http_server(404)
    try:
        result = request("POST", f"http://127.0.0.1:{server.server_port}/", "token", allow_forbidden=True)
    finally:
        server.shutdown()
    assert result is None


def test_request_still_raises_on_other_errors_when_forbidden_allowed(monkeypatch):
    """allow_forbidden is specifically about 403/404 - a persistent 500 is
    still a genuine failure and must not be swallowed."""
    monkeypatch.setattr(gh_client.time, "sleep", lambda *a: None)
    server = _local_http_server(500)
    try:
        with pytest.raises(SystemExit):
            request("POST", f"http://127.0.0.1:{server.server_port}/", "token", allow_forbidden=True)
    finally:
        server.shutdown()


def test_request_retries_transient_server_errors_and_succeeds(monkeypatch):
    """A single transient 502/503 shouldn't fail the whole job - request()
    should retry and return the eventual successful response."""
    monkeypatch.setattr(gh_client.time, "sleep", lambda *a: None)
    server, attempts = _flaky_then_ok_server(fail_status=503, fail_times=2)
    try:
        result = request("GET", f"http://127.0.0.1:{server.server_port}/", "token")
    finally:
        server.shutdown()
    assert result == {"ok": True}
    assert len(attempts) == 3


def test_request_gives_up_after_max_attempts(monkeypatch):
    monkeypatch.setattr(gh_client.time, "sleep", lambda *a: None)
    server, attempts = _flaky_then_ok_server(fail_status=502, fail_times=99)
    try:
        with pytest.raises(SystemExit, match="failed after 3 attempts"):
            request("GET", f"http://127.0.0.1:{server.server_port}/", "token")
    finally:
        server.shutdown()
    assert len(attempts) == gh_client.MAX_ATTEMPTS


def test_request_wraps_connection_failures_in_systemexit(monkeypatch):
    """DNS/TLS/connection failures used to escape as a raw traceback instead
    of the readable SystemExit message every other failure mode gets."""
    monkeypatch.setattr(gh_client.time, "sleep", lambda *a: None)
    # Nothing listens on this port - urlopen raises URLError(ConnectionRefusedError).
    with pytest.raises(SystemExit, match="failed after 3 attempts"):
        request("GET", "http://127.0.0.1:1/", "token")


def test_redirect_handler_strips_authorization_header_on_redirect():
    """Regression test: GitHub's archive_download_url 302s to a signed
    blob-storage URL that rejects a forwarded GitHub Authorization header
    with a 401. Confirms our custom redirect handler strips it, using a
    real local HTTP server + real urllib redirect handling (not mocked)."""
    seen_auth_on_final = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path == "/initial":
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{server.server_port}/final")
                self.end_headers()
            elif self.path == "/final":
                seen_auth_on_final.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ok")

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{server.server_port}/initial")
        req.add_header("Authorization", "Bearer secret-token")
        opener = urllib.request.build_opener(StripAuthOnRedirect)
        with opener.open(req, timeout=5) as resp:
            body = resp.read()
    finally:
        server.shutdown()

    assert body == b"ok"
    assert seen_auth_on_final == [None]


def test_request_sends_json_body_with_content_type_header():
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen["content_type"] = self.headers.get("Content-Type")
            length = int(self.headers.get("Content-Length", 0))
            seen["body"] = json.loads(self.rfile.read(length))
            response = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = request("POST", f"http://127.0.0.1:{server.server_port}/", "token", body={"a": 1})
    finally:
        server.shutdown()
    assert result == {"ok": True}
    assert seen["content_type"] == "application/json"
    assert seen["body"] == {"a": 1}


def test_request_raw_returns_bytes_without_json_parsing():
    body_bytes = b"not json, just a zip blob"

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = request("GET", f"http://127.0.0.1:{server.server_port}/", "token", raw=True)
    finally:
        server.shutdown()
    assert result == body_bytes
