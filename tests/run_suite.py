"""Run scoring tests, public-pool tests, and migration tests on SQLite and Postgres."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def run(script: str, env: dict) -> None:
    print(f"\n=== {script} ===", flush=True)
    subprocess.check_call([PY, str(ROOT / "tests" / script)], cwd=str(ROOT), env=env)


def base_env() -> dict:
    env = os.environ.copy()
    env["SECRET_KEY"] = "test-secret"
    env["ADMIN_PASSWORD"] = "test-admin"
    env.pop("DATABASE_URL", None)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def sqlite_pass() -> None:
    mig = base_env()
    mig["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-mig-")) / "legacy.db")
    run("test_migration.py", mig)
    feat = base_env()
    feat["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-feat-")) / "sweep.db")
    run("test_public.py", feat)
    night = base_env()
    night["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-night-")) / "sweep.db")
    run("test_night.py", night)
    weekend = base_env()
    weekend["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="sweep-weekend-")) / "sweep.db")
    run("test_weekend.py", weekend)


def postgres_pass() -> None:
    os.environ.setdefault("XDG_RUNTIME_DIR", "/tmp/runtime-sweep")
    Path(os.environ["XDG_RUNTIME_DIR"]).mkdir(parents=True, exist_ok=True)
    from embedded_postgres import get_server
    import psycopg

    parent = Path(tempfile.mkdtemp(prefix="pg-parent-"))
    server = get_server(parent / "pgdata", cleanup_mode="delete")
    admin = psycopg.connect(server.get_uri("postgres"), autocommit=True)
    try:
        admin.execute("CREATE DATABASE sweep_mig")
        admin.execute("CREATE DATABASE sweep_feat")
        admin.execute("CREATE DATABASE sweep_night")
        admin.execute("CREATE DATABASE sweep_weekend")
    finally:
        admin.close()
    try:
        mig = base_env()
        mig["DATABASE_URL"] = server.get_uri("sweep_mig")
        mig.pop("DB_PATH", None)
        run("test_migration.py", mig)
        feat = base_env()
        feat["DATABASE_URL"] = server.get_uri("sweep_feat")
        feat.pop("DB_PATH", None)
        run("test_public.py", feat)
        night = base_env()
        night["DATABASE_URL"] = server.get_uri("sweep_night")
        night.pop("DB_PATH", None)
        run("test_night.py", night)
        weekend = base_env()
        weekend["DATABASE_URL"] = server.get_uri("sweep_weekend")
        weekend.pop("DB_PATH", None)
        run("test_weekend.py", weekend)
    finally:
        server.cleanup()


def main():
    env = base_env()
    run("test_scoring.py", env)
    print("\n=== sqlite ===", flush=True)
    sqlite_pass()
    print("\n=== postgres ===", flush=True)
    postgres_pass()
    print("\n=== gunicorn smoke ===", flush=True)
    run("smoke_gunicorn.py", base_env())
    print("\nALL SUITES PASSED")


if __name__ == "__main__":
    main()
