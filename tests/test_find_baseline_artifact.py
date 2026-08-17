import http.server
import threading
import urllib.request

from find_baseline_artifact import _StripAuthOnRedirect, select_artifact


def artifact(name="sonar-baseline-abc123", expired=False, created_at="2026-01-01T00:00:00Z", **extra):
    return {"name": name, "expired": expired, "created_at": created_at, **extra}


def test_select_artifact_returns_none_when_no_artifacts():
    assert select_artifact([], "sonar-baseline-abc123") is None


def test_select_artifact_ignores_non_matching_names():
    artifacts = [artifact(name="sonar-baseline-other")]
    assert select_artifact(artifacts, "sonar-baseline-abc123") is None


def test_select_artifact_ignores_expired_artifacts():
    artifacts = [artifact(expired=True)]
    assert select_artifact(artifacts, "sonar-baseline-abc123") is None


def test_select_artifact_picks_the_single_match():
    artifacts = [artifact()]
    assert select_artifact(artifacts, "sonar-baseline-abc123") == artifacts[0]


def test_select_artifact_prefers_newest_on_collision():
    older = artifact(created_at="2026-01-01T00:00:00Z", id=1)
    newer = artifact(created_at="2026-06-01T00:00:00Z", id=2)
    result = select_artifact([older, newer], "sonar-baseline-abc123")
    assert result["id"] == 2


def test_select_artifact_skips_expired_even_if_only_candidate_with_matching_name():
    artifacts = [
        artifact(expired=True, id=1),
        artifact(name="sonar-baseline-other", expired=False, id=2),
    ]
    assert select_artifact(artifacts, "sonar-baseline-abc123") is None


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
        opener = urllib.request.build_opener(_StripAuthOnRedirect)
        with opener.open(req, timeout=5) as resp:
            body = resp.read()
    finally:
        server.shutdown()

    assert body == b"ok"
    assert seen_auth_on_final == [None]
