"""Service-worker shell must not stick across a deploy. Temp SQLite only."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if not (os.environ.get("DATABASE_URL") or "").strip() and not (os.environ.get("DB_PATH") or "").strip():
    os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-shell-")) / "sweep.db")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("ADMIN_PASSWORD", "test-admin")

sys.path.insert(0, str(ROOT))
from app import app  # noqa: E402
import sweepstakes_flask as S  # noqa: E402

CLIENT = app.test_client()

HTML_PAGES = (
    "/",
    "/join",
    "/live",
    "/compare",
    "/admin",
    "/open",
    "/open/live",
    "/recap",
    "/open/recap",
    "/coalition",
    "/demo/live",
    "/p/not-a-real-token",
)


def test_sw_cache_name_follows_deploy():
    os.environ["RENDER_GIT_COMMIT"] = "abc123deploy"
    try:
        first = CLIENT.get("/sw.js")
        second = CLIENT.get("/sw.js")
    finally:
        os.environ.pop("RENDER_GIT_COMMIT", None)
    assert first.status_code == 200
    assert first.mimetype == "application/javascript"
    assert first.headers["Cache-Control"] == "no-cache"
    assert first.headers["Service-Worker-Allowed"] == "/"
    body = first.get_data(as_text=True)
    assert body == second.get_data(as_text=True)
    assert "__SW_CACHE_NAME__" not in body
    assert 'const CACHE = "sweep-shell-abc123deploy";' in body
    assert "skipWaiting" in body
    assert "clients.claim" in body
    assert "caches.delete" in body
    assert "request.mode === \"navigate\"" in body
    assert "text/html" in body
    assert "cache: \"reload\"" in body
    assert "networkFirst" in body
    # Cache-first stale-while-revalidate is gone.
    assert "return cached ||" not in body
    # API, including SSE, is left to the network.
    assert 'url.pathname.startsWith("/api/")' in body
    fallback = CLIENT.get("/sw.js")
    fallback_body = fallback.get_data(as_text=True)
    assert "sweep-shell-abc123deploy" not in fallback_body
    assert "sweep-shell-" in fallback_body
    assert S.shell_cache_name() in fallback_body


def test_html_and_static_revalidate():
    for path in HTML_PAGES:
        resp = CLIENT.get(path)
        assert resp.status_code == 200, path
        assert resp.mimetype == "text/html", path
        assert resp.headers["Cache-Control"] == "no-cache", path
    css = CLIENT.get("/static/css/app.css")
    assert css.status_code == 200
    assert css.mimetype == "text/css"
    assert css.headers["Cache-Control"] == "no-cache"
    js = CLIENT.get("/static/js/common.js")
    assert js.status_code == 200
    assert js.headers["Cache-Control"] == "no-cache"
    script = js.get_data(as_text=True)
    assert "controllerchange" in script
    assert "sw-controller-reload-at" in script
    assert "location.reload" in script
    manifest = CLIENT.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.headers["Cache-Control"] == "no-cache"


def test_api_stays_json():
    state = CLIENT.get("/api/state")
    assert state.status_code == 200
    assert state.is_json
    assert state.mimetype != "text/html"
    health = CLIENT.get("/healthz")
    assert health.status_code == 200
    assert health.is_json
    assert health.get_json()["ok"] is True


if __name__ == "__main__":
    test_sw_cache_name_follows_deploy()
    test_html_and_static_revalidate()
    test_api_stays_json()
    print("SHELL CACHE TESTS PASSED")
