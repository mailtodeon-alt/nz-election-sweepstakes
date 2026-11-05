# NZ Election Night Sweepstakes 2026 — Admin guide

## Where things live

- App root: `./`
- Real database: `data/sweepstakes.db`
- Demo database: `data/demo.db` (isolated)
- Admin password: set `ADMIN_PASSWORD` env / App Setting (never commit it). Local box may still have `ADMIN_PASSWORD.txt` for Yoda’s machine only — **not** in the Azure zip.
- Public URL: `PUBLIC_URL.txt` (changes if the tunnel restarts)
- Restart: `./start.sh`
- Logs: `data/server.log`, `data/tunnel.log`

## Open the app

1. Read the current public URL from `PUBLIC_URL.txt`.
2. Home: `/` · Join: `/join` · TV board: `/live` · Compare: `/compare` · Admin: `/admin`
3. Public (not in the pot): sign-up `/open` · public board `/open/live` · each player still uses `/p/<token>`
4. Demo copies of the same pages: `/demo/`, `/demo/live`, `/demo/admin`, `/demo/open`, `/demo/open/live`

## First-time setup (before the party)

1. Open `/admin` and log in with the `ADMIN_PASSWORD` app setting.
2. Rename **Player 1…6** to your friends’ names (click out of the field to save).
3. Copy each personal link (`/p/<token>`) and send it privately (Signal/text). Each link is unique — that is what stops duplicate entries.
4. Optionally tweak questions (add TOP/Opportunity, turnout, “number of parties”, etc.) **before** locking.
5. Confirm lock time (default **7:00pm NZDT, Sat 7 Nov 2026**). Server enforces Pacific/Auckland including DST.

## Election night

1. Put `/live` on the TV (browser fullscreen). Widescreen 16:9 works best.
2. At 7:00pm entries auto-lock. You can also press **Lock now**.
3. As Electoral Commission / TV numbers come in, enter them under **Manual results** and hit **Save results & recalc**. Fields left blank stay **PENDING** (not scored as zero).
4. Manual values are marked `MANUAL` and are never overwritten by the auto feed.
5. Around 11:30pm (or whenever you celebrate), press **Freeze election-night snapshot** — board shows **FROZEN ELECTION NIGHT**.
6. When official results publish (~27 Nov 2026), enter them and press **Mark official snapshot**.

## Auto feed (optional)

- Documented live XML base: `https://media.election.net.nz/xml/`
- Archives (past cycles): `https://archive.electionresults.govt.nz/electionresults_YYYY/xml/`
- **Default is OFF.** The 2026 feed 404s until counting starts; the exact field layout is not verified for 2026 in this build. Manual entry is fully sufficient.
- If you enable it on the night and it parses cleanly, values arrive as `AUTO`. Your manual overrides still win.
- Poll interval: 60–120s recommended (feed CDN cache ~30s).

## Public players (not in the pot)

Home and Join offer **Play for fun (not in the pot)**. Those people are stored with `pool=public`. They answer the same 10 questions, use the same scoring and tie-break, and lock at the same time. They never appear on `/live`, `/compare`, the pot prize panel, frozen/official snapshots, or the pot CSV.

In **Admin → Public players**:

- See each public player, whether they have submitted, and their score.
- Rename or **Remove** (hides them and frees the name; the row stays in the database so nothing is deleted).
- **Public sign-ups open** — default on. Off refuses new sign-ups with 403. The lock at 7:00pm also refuses sign-ups and edits.
- **Cap** — default 200 active public players.
- **Export public CSV** — public results only, no prize column. The pot **Export CSV** stays the six friends.

Sign-ups are limited to 5 per IP per 10 minutes (in memory, one gunicorn worker). Names are 1–24 characters, unique ignoring case, and HTML-escaped before they are stored.

On each boot the app adds a `players.pool` column if the live database does not have one. Existing rows default to `pot`. Tokens, names, predictions, and results are not rewritten. Running the migration again does not move a `public` player back into the pot.

## Demo mode

1. From admin: **Reset demo DB**, then **Demo stage 1…4** to simulate counting.
2. Open `/demo/live` — orange DEMO frame; never mixes with the real database.
3. Use `/demo/admin` (same password) if you need demo-specific player edits.

## If the public link dies

The Cloudflare quick tunnel URL changes on restart.

```bash
cd /workspace/sweepstakes
# stop old processes if needed
kill $(cat data/server.pid) $(cat data/tunnel.pid) 2>/dev/null || true
./start.sh
cat PUBLIC_URL.txt
```

Box uptime: if this computer sleeps or reboots, the app and tunnel stop until `./start.sh` is run again.

## Scoring reminders (shown to players)

- Party vote bands (Q1–6): exact band 10 pts, adjacent band 4 pts. Boundaries (one decimal) fall in the **higher** band.
- Most seats (Q7): National / Labour / Other — 5 pts.
- Parties in Parliament (Q8): 4 / 5 / 6 / 7 / 8+ — exact 10, adjacent 5.
- NAT+ACT+NZF 61+ (Q9): Yes/No — 15 pts.
- Highest minor (Q10): ACT / Green / NZ First / Te Pāti Māori / TOP — 10 pts.
- Tie-break: closest numeric guess at National's exact party vote % (entered on the ballot; not part of the 100).
- Prizes: 1st $100 · 2nd $20 · last place **On morning dishes** 🍽️
- Admin enters a party vote%/seats table; Q7–Q10 derive automatically (overridable).

## Add to Home Screen (PWA)

Players can install their personal ballot from `/p/<token>`:

- **iPhone:** Share → Add to Home Screen
- **Android:** Chrome install prompt / menu → Install app

The installed icon opens that player's own entry page (standalone, dark theme).

**Important:** Cloudflare quick-tunnel URLs change whenever the tunnel restarts. An installed home-screen icon pointed at an old `*.trycloudflare.com` URL will break. After a restart, open the new public URL, go to the personal link again, and re-add to home screen. Prefer keeping the tunnel up for the whole evening.

## Azure

On App Service, configure `ADMIN_PASSWORD`, `DB_PATH=/home/data/sweepstakes.db`, and Always On (B1). See **DEPLOY_AZURE.md**. First boot creates a fresh database and new player tokens automatically.

## Render

See **RENDER_NOTES.md**. Free web sleeps after ~15 min idle — open the site before guests arrive. Free Postgres expires ~30 days after creation (check current Render docs). Data lives in Postgres via `DATABASE_URL`.
