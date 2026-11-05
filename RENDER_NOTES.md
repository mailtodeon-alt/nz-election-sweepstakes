# Render notes — Election Sweepstakes 2026

Personal/social app. Public internet, **no** Easy Auth / Entra. Access control = unguessable `/p/<token>` links + `ADMIN_PASSWORD`.

## Free tier behaviour (verify on [render.com/docs/free](https://render.com/docs/free))

- **Web service (free):** spins down after **~15 minutes** without inbound HTTP traffic. Next request cold-starts in about **one minute** (loading page shown). Open the site **~5 minutes before guests arrive** so the TV board is warm.
- **Free Postgres:** **expires 30 days after creation** (then a grace period to upgrade before deletion — currently **14 days** per Render docs). 1 GB storage; one free DB per workspace. Upgrade before election night if the DB is near expiry, or recreate and re-enter/export scores.
- Ephemeral filesystem: **do not rely on SQLite on Render free**. The Blueprint wires `DATABASE_URL` to free Postgres.

## Deploy

1. Connect GitHub (parent will handle) and create a Blueprint from `render.yaml`, or push this repo and “New → Blueprint”.
2. When prompted, set **ADMIN_PASSWORD**.
3. `SECRET_KEY` is auto-generated; `DATABASE_URL` comes from the linked DB.
4. Region: **singapore** (closest listed region to NZ).

## Rotate admin password

Dashboard → web service → Environment → edit `ADMIN_PASSWORD` → Save (redeploy/restart). Old sessions become invalid after restart when the cookie signer / login check uses the new password (login only; sessions stay until cookie expires — force logout by changing `SECRET_KEY` too if needed).

## Public players migration

The first boot after this change adds `players.pool` (`TEXT NOT NULL DEFAULT 'pot'`) on Postgres and on local SQLite. Existing pot rows, tokens, and predictions are left in place. Public sign-ups use the same database and a separate leaderboard (`/open`, `/open/live`). The migration is idempotent and does not reset `pool` on later restarts.

## Demo mode

`/demo/live` and admin **Demo** controls use an isolated Postgres schema `demo` (or `demo.db` locally). Reset anytime; never mixes with real results. Orange DEMO frame on the TV board.

## Local run

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export ADMIN_PASSWORD=dev
export SECRET_KEY=dev-secret-not-for-prod
# optional: export DATABASE_URL=postgresql://...
gunicorn -k gthread --threads 16 -w 1 --timeout 0 -b 0.0.0.0:8787 app:app
```

## Local Postgres validation

The psycopg dual-backend path was validated on this box with **embedded-postgres** (apt/docker unavailable). Full suite against `DATABASE_URL` passed: unit scoring, healthz (backend=postgres), admin login, 6 concurrent submissions, lock 403, party-table derive, snapshot freeze, CSV export, SSE hello/heartbeat/reconnect, demo reset/step.
