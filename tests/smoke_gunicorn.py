"""Boot one gunicorn gthread worker and walk the public-player paths."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _call(opener, method, url, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main():
    port = _free_port()
    db_path = Path(tempfile.mkdtemp(prefix="sweep-smoke-")) / "sweep.db"
    env = os.environ.copy()
    env.pop("DATABASE_URL", None)
    env["DB_PATH"] = str(db_path)
    env["SECRET_KEY"] = "smoke-secret"
    env["ADMIN_PASSWORD"] = "smoke-admin"
    env["PORT"] = str(port)
    log_path = Path(tempfile.mkdtemp(prefix="sweep-smoke-log-")) / "gunicorn.log"
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "gunicorn",
            "-k", "gthread", "--threads", "4", "-w", "1", "--timeout", "0",
            "-b", f"127.0.0.1:{port}", "app:app",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        health = None
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"gunicorn exited early\n{log_path.read_text()[-2000:]}")
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=2) as resp:
                    health = json.loads(resp.read().decode())
                    break
            except Exception:
                time.sleep(0.3)
        if not health or not health.get("ok"):
            raise RuntimeError(f"healthz failed\n{log_path.read_text()[-2000:]}")
        assert health["backend"] == "sqlite"

        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())
        for path in ("/", "/join", "/open", "/open/live", "/live", "/compare"):
            status, body = _call(opener, "GET", base + path)
            assert status == 200, path
            text = body.decode()
            if path == "/":
                assert "Play for fun (not in the pot)" in text
            if path == "/live":
                assert "morning dishes" in text
            if path == "/open/live":
                assert "morning dishes" not in text.lower()
                assert "$100" not in text

        status, body = _call(opener, "POST", base + "/api/open/signup", {"name": "Smoke Sam"})
        assert status == 200, body
        created = json.loads(body.decode())
        token = created["player"]["token"]
        assert created["player"]["link"] == f"/p/{token}"

        status, body = _call(opener, "GET", base + f"/p/{token}")
        assert status == 200
        status, body = _call(opener, "GET", base + f"/api/player/{token}/manifest.webmanifest")
        manifest = json.loads(body.decode())
        assert manifest["start_url"] == f"/p/{token}"
        assert "Smoke Sam" in manifest["name"]

        status, body = _call(opener, "GET", base + "/api/state")
        pot = json.loads(body.decode())
        assert pot["pool"] == "pot"
        assert len(pot["players"]) == 6
        assert all(p["name"] != "Smoke Sam" for p in pot["players"])

        status, body = _call(opener, "GET", base + "/api/open/state")
        public = json.loads(body.decode())
        assert [r["name"] for r in public["leaderboard"]] == ["Smoke Sam"]

        status, _ = _call(opener, "GET", base + "/api/admin/export.csv")
        assert status == 401
        status, _ = _call(opener, "GET", base + "/api/admin/export-public.csv")
        assert status == 401
        status, body = _call(opener, "POST", base + "/api/admin/login", {"password": "smoke-admin"})
        assert status == 200, body
        status, body = _call(opener, "GET", base + "/api/admin/export.csv")
        pot_csv = body.decode()
        assert status == 200
        assert "Smoke Sam" not in pot_csv
        assert "Player 1" in pot_csv
        status, body = _call(opener, "GET", base + "/api/admin/export-public.csv")
        pub_csv = body.decode()
        assert status == 200
        assert "Smoke Sam" in pub_csv
        assert "prize" not in pub_csv.splitlines()[0].split(",")
        print(f"GUNICORN SMOKE OK on {base}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log.close()


if __name__ == "__main__":
    main()
