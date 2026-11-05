# NZ Election Night Sweepstakes 2026

Private sweepstakes for six friends — mobile entry, live TV board, admin results, demo mode.

**Deploy target:** Render free web service + free Postgres (`render.yaml`). See **RENDER_NOTES.md**.

Personal/social only — no Lower Clothing / business data.

## Questions (100 points)

| # | Question | Scoring |
|---|----------|---------|
| 1–6 | Party vote bands (NAT/LAB/GRN/ACT/NZF/TPM) | Exact band 10, adjacent 4 |
| 7 | Most seats | National / Labour / Other — 5 |
| 8 | Parties in Parliament | 4/5/6/7/8+ — exact 10, adjacent 5 |
| 9 | NAT+ACT+NZF 61+ | Yes/No — 15 |
| 10 | Highest minor | ACT/Green/NZ First/Te Pāti Māori/TOP — 10 |

Band boundaries (one decimal) fall in the **higher** band. Tie-break: closest National exact %. Prizes: 1st $100 · 2nd $20 · last **On morning dishes** 🍽️.

## Config

| Env | Required | Notes |
|-----|----------|-------|
| `ADMIN_PASSWORD` | Yes | Admin login |
| `SECRET_KEY` | Yes | Flask sessions |
| `DATABASE_URL` | On Render | Postgres; omit locally → SQLite |
| `PORT` | Set by Render | |

Start: `gunicorn -k gthread --threads 16 -w 1 --timeout 0 -b 0.0.0.0:$PORT app:app` (single worker — SSE + shared memory).

## Health

`GET /healthz` → `{ok: true, backend: "postgres"|"sqlite"}`.
