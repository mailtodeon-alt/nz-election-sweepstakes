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

## Public play (not in the pot)

Anyone with the site link can play the same 10 questions on a separate leaderboard. They are not in the $120 pot.

| Page | URL |
|------|-----|
| Sign up | `/open` |
| Public leaderboard | `/open/live` |
| Personal ballot | `/p/<token>` (issued at sign-up) |
| Demo sign-up | `/demo/open` |
| Demo public board | `/demo/open/live` |

Home (`/`) and Join (`/join`) link to this as **Play for fun (not in the pot)**. Names are 1–24 characters, unique among public players (case-insensitive), and stored HTML-escaped. Sign-ups and edits stop when entries lock. Default cap is 200 public players (admin can change it or close sign-ups). Per-IP limit is 5 sign-ups per 10 minutes on the single worker.

`/live`, `/compare`, pot prizes, snapshots, and the pot CSV include only the six pot players. Startup adds `players.pool` (`pot` or `public`) if it is missing; existing rows become `pot` and keep their tokens and predictions. Startup does not rename players, replace tokens, or rewrite questions, settings, or the 60 stored answers.

## Election night

Preview everything on the demo TV first: Admin → **Reset demo DB**, then **Demo stage 1**, then open `/demo/live`.

| Feature | Where | Night-of note |
|---------|--------|----------------|
| Reactions | Emoji row on each ballot and on `/open/live`. Floats on `/live` and `/open/live`. | One tap a second. Bursts collapse into one update. Admin can switch reactions off. |
| Side bets | Admin creates the question, options, and points, then closes it and sets the answer. Players answer on their ballot while it is open. | Points are added on the public board only, and listed in the side-bet table on `/live`. Pot places, prizes, the frozen snapshot, and the pot CSV stay on the main ballot. |
| Score chart | **Chart** on `/live` and `/open/live` (`?view=history`). | A point is stored when results are saved. The axis is % counted when that was entered. Public chart shows at most 10 lines. |
| Ticker | Bottom of both boards. Players post from their ballot (80 characters, one a minute). | Public lines stay on `/open/live` unless you press **Feature on TV**. Hide, delete, or switch the ticker off in admin. |
| Group vs public | Panel on both boards. | Average of the main ballot for people who submitted. Side bets are not in this average. |
| Results feed | Admin → Results feed. Leave it **off** until the XML is live. | **Dry run / preview** fetches votes and seats and does not save them. Manual numbers always win. A bad or empty fetch does not clear what you typed. New databases default to a 3 minute interval; an existing database keeps its saved interval (often 90) until you change it. |

## After election night

Turn on **Weekend mode** in admin when the TV should rotate Board, Latest count, Estimate, and Ranks. The main board stays the frozen pot result once you have taken the 11:30pm snapshot. Later edits and the feed update **Latest count** only.

| Page | What it is |
|------|------------|
| `/live` tabs, or `/live?view=latest` | Latest count: right / adjacent / wrong on the 10 calls, plus counted % over time. Labelled as not the pot result. |
| `/live` Estimate tab | Special-vote seat sketch. Defaults use the 2023 preliminary-to-official shift (603,257 special votes). Sainte-Laguë, 120 seats, overhang noted. |
| `/live` Ranks tab, `/live?view=history` | Place from Saturday night to now, and the biggest mover. |
| `/recap`, `/recap.png` | Pot recap card (1080×1350) and PNG. |
| `/open/recap`, `/open/recap.png` | Same card for the public board. |
| `/coalition` | Host-written coalition notes. |
| `/demo/live`, `/demo/recap`, `/demo/coalition` | Preview. Demo weekend mode is on after a demo reset. |

Bonus rounds are extra side bets (closest number, with an open-until time). Points land on the public board and the bonus table only.

## Health

`GET /healthz` → `{ok: true, backend: "postgres"|"sqlite"}`.
