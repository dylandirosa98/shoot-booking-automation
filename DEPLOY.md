# Deployment & Setup Guide

This doc grows as we build. It captures **everything** you need to:
1. Run the app locally
2. Deploy it to Railway
3. Connect external services (Google, Pipedrive)

---

## 1. Local development

```bash
cd ~/videographer-scheduling-automation
source venv/bin/activate
cp .env.example .env          # already done; edit values as needed
python manage.py migrate
python manage.py seed_videographers           # load the 24 videographer roster (lat/lng baked in)
python manage.py createsuperuser              # for accessing /admin
python manage.py runserver
```

Then visit:
- `http://127.0.0.1:8000/` — dashboard (your friend's view)
- `http://127.0.0.1:8000/admin` — Django admin (data management)

---

## 2. Environment variables

See `.env.example` for the full list with comments. Summary:

| Variable | Where to get it | Required? |
|---|---|---|
| `SECRET_KEY` | Generate: `python -c "import secrets; print(secrets.token_urlsafe(50))"` | Yes (prod) |
| `DEBUG` | `False` in prod, `True` locally | Yes |
| `ALLOWED_HOSTS` | Your domain(s) | Prod only |
| `DATABASE_URL` | Auto-set by Railway when Postgres is attached | Prod only |
| `GOOGLE_OAUTH_CLIENT_ID` | Google Cloud Console (see section 5) | Yes |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Same as above | Yes |
| `GOOGLE_OAUTH_REFRESH_TOKEN` | Generated locally one time (see section 5) | Yes |
| `GOOGLE_CALENDAR_OWNER_EMAIL` | Your friend's company Google email | Yes |
| `PIPEDRIVE_WEBHOOK_SECRET` | You make this up; configure in Pipedrive | Yes |
| `PIPEDRIVE_API_TOKEN` | Pipedrive settings -> Personal Preferences -> API | Yes for Facebook lead ingest |
| `FACEBOOK_VERIFY_TOKEN` | You invent this; paste into Meta webhook config | Yes for Instant Forms |
| `FACEBOOK_APP_SECRET` | Meta app Settings -> Basic | Yes for Instant Forms |
| `FACEBOOK_PAGE_ACCESS_TOKEN` | Graph API Explorer / system user | Yes for Instant Forms |
| `CLICKUP_API_TOKEN` | ClickUp personal API token | Yes for edit task creation |
| `ESCALATION_HOURS` | Default 24 | No |
| `SCORE_PENALTY_PER_MINUTE` | Tuning knob, default 0.01 | No |
| `MAX_DRIVE_MINUTES` | Hard cap, default 180 | No |
| `NOTIFY_EMAIL` | Where to send "shoot booked!" alerts | Yes |

---

## 3. Railway deployment (do this AFTER local works)

1. Push the project to a GitHub repo.
2. Go to https://railway.app → New Project → Deploy from GitHub Repo.
3. Add the **PostgreSQL** plugin (one click). `DATABASE_URL` is auto-injected.
4. In Railway → Variables tab, paste every env var from your `.env`
   (EXCEPT `DATABASE_URL` — Railway owns that).
   Important: set `DEBUG=False` and add your Railway URL to `ALLOWED_HOSTS`.
5. Railway auto-detects Python via `requirements.txt`. Add a `Procfile` (already in repo).
6. Railway's start command already runs:
   ```
   python manage.py migrate --noinput
   python manage.py collectstatic --noinput
   python manage.py bootstrap_prod
   ```
   The edit-job migrations and one-time editor ranking seed run during `migrate`.
7. If you still need an admin user, open Railway shell and run:
   ```
   python manage.py createsuperuser
   ```
8. Visit `https://<your-app>.railway.app/admin` and log in.

---

## 4. Distance + geocoding (no API key needed)

We use:
- **Haversine formula** (straight-line distance from lat/lng, multiplied by 1.3 for road detour, divided by 45 mph for time). Accurate within ~15% of Google.
- **Nominatim** (OpenStreetMap) — free, no key — to convert shoot addresses to lat/lng.
- Videographer lat/lng is baked into the seed file (city centers).

If you ever want exact Google Maps distances later, swap `scheduler/distance.py` for a Google Distance Matrix call. Not needed for v1.

---

## 5. Google Calendar OAuth (the annoying one)

Goal: get a **refresh token** that lets our app create calendar events as your
friend's company Google account, without re-logging in.

### One-time setup

1. Same Google Cloud project as above.
2. APIs & Services -> Library -> enable **Google Calendar API** and **Google Drive API**.
3. APIs & Services -> OAuth consent screen:
   - User Type: External
   - App name: "Hockey Shoot Scheduler" (or whatever)
   - Add your friend's email as a test user
   - Scopes: add `https://www.googleapis.com/auth/calendar.events`, `https://www.googleapis.com/auth/gmail.send`, and `https://www.googleapis.com/auth/drive.file`
4. Credentials -> Create credentials -> OAuth client ID:
   - Application type: Desktop app
   - Download the JSON. Copy `client_id` and `client_secret` into `.env`.
5. Run the OAuth helper:
   ```
   python manage.py get_google_refresh_token
   ```
   It opens a browser; sign in as the company Google account and grant the requested permissions. It saves a new refresh token into `.env`.

After this, the app can create calendar events and Drive shoot folders. The Drive share is sent automatically when a videographer accepts.

Optional: create a parent folder in Drive for all shoots and set `GOOGLE_DRIVE_PARENT_FOLDER_ID` to the ID in that folder's URL. Leave it blank to create folders in My Drive.

Refresh tokens can be revoked or expire after six months of non-use.

---

## 6. Pipedrive webhook

1. Pipedrive -> Settings -> Tools and apps -> Webhooks.
2. Add new webhook:
   - Event: `updated.deal` (or whichever event corresponds to "shoot booked")
   - URL: `https://<your-app>.railway.app/webhook/pipedrive/`
   - HTTP Auth (optional but recommended): set a username + password.
     Combine into a secret and put in `PIPEDRIVE_WEBHOOK_SECRET`.
3. Test by moving a deal to the "Shoot Booked" stage in Pipedrive.

**⚠️ Before going to production:** Set `PIPEDRIVE_WEBHOOK_SECRET=user:password`
in env, configure the same credentials in Pipedrive's webhook HTTP auth fields.
Without this, anyone who finds the URL can POST fake payloads.

### Activity field usage (tell your friend)

When creating a Shoot Booking activity in Pipedrive, the activity UI has two
text areas: "Notes" and "Description". **Only Description is sent in v2 webhooks.**
Always put shoot info (special requests, equipment, contact details, etc.)
in the **Description** field. Anything typed in Notes will NOT reach the videographer.

For v2 activity webhooks, Pipedrive sends this as `public_description`.

---

## 7. Facebook Instant Forms -> Pipedrive

This is **not** the Pipedrive activity webhook. Facebook POSTs to us; we write
to Pipedrive with the API: Person (find-or-create) + Lead + pinned Note of every
form answer.

Callback URL to give Meta:

```
https://<your-app>.railway.app/webhook/facebook/
```

1. Create a Meta app (Business type) and add the **Webhooks** product.
2. Subscribe to object **Page**, field **`leadgen`**.
3. Verify token = `FACEBOOK_VERIFY_TOKEN`. The GET handshake must succeed
   before Meta will save the callback URL.
4. Install the app on the Page:

```
curl -X POST "https://graph.facebook.com/v22.0/{page-id}/subscribed_apps" \
  -d "subscribed_fields=leadgen" \
  -d "access_token={page-access-token}"
```

5. Put a long-lived **Page** access token in `FACEBOOK_PAGE_ACCESS_TOKEN`.
   A Business Manager system user token is best so it does not expire.
6. In Meta Business Suite -> Integrations -> Leads Access, assign this app
   if Lead Access Manager is enabled.
7. Set `PIPEDRIVE_API_TOKEN` (required for the write). Optional:
   `PIPEDRIVE_LEAD_OWNER_ID`, `PIPEDRIVE_FACEBOOK_LABEL_ID`.
8. Test with Meta's Lead Ads Testing Tool, or locally:

```
python manage.py simulate_facebook_lead --name "Jane Doe" --email jane@example.com
```

The webhook payload is IDs only. The app then fetches
`/{leadgen_id}?fields=field_data,...` and stores a `FacebookLead` row so Meta
retries do not create duplicate Pipedrive leads.

## 8. ClickUp edit tasks

The edit automation listens for Pipedrive activities whose type is exactly one of:

- `Recruiting Highlight Video`
- `Hype Video`
- `Highlight Recap`

When one arrives, the app chooses an editor using the per-video-type ranking and
that editor's `max_active_jobs` threshold. It then creates a ClickUp task in the
Editing Projects list using:

- Pipedrive activity subject -> ClickUp task title
- Pipedrive due date -> ClickUp due date (date-only)
- Pipedrive activity Description -> ClickUp description
- selected editor's `clickup_user_id` -> ClickUp assignee

A one-time migration seeds the initial editor rows, ClickUp user IDs, and rankings.
Django records that migration, so it will not run again on later deploys.

## 9. Game Opportunities (`/opportunities/`)

The **Game Opportunities** tab (`/opportunities/`) requires staff login through `/admin/login/`. It displays upcoming shoot days and games found at the same facilities/dates in the configured seasons on five target sites:

1. **AYHL** (`https://atlantichockey.org/ayhl.php`)
2. **GameSheet** (`https://www.gamesheetinc.com/`)
3. **AHF** (`https://atlantichockeyfederation.com/`)
4. **THF** (`https://www.tier1hockeyfederation.com/`)
5. **FedHockey / EHF** (`https://fedhockey.com/`)

along with team labels and candidate CRM matches (**Direct Matches** + **Maybe Matches**). See the coverage limitations below.

### Weekly automation (every Wednesday)

A user-level systemd timer runs the full refresh every **Wednesday at 6:00 AM ET** (`Persistent=true`, so a missed run happens as soon as the computer is back on):

```bash
systemctl --user list-timers game-opportunities-weekly.timer   # next run
systemctl --user start game-opportunities-weekly.service       # run now
journalctl --user -u game-opportunities-weekly.service -f      # watch
ls var/logs/                                                   # per-run logs (last 26 kept)
```

Unit files live in `deploy/systemd/` (installed copies in `~/.config/systemd/user/`). The service runs `scripts/run_weekly_opportunities.sh`, which executes:

```bash
python manage.py research_game_opportunities --use-vibe --push --fail-on-partial
```

Pipeline: Pipedrive (upcoming `shoot_booking` activities + all deals/leads/persons/notes) → schedule collectors for the shoot days → Mistral Vibe web research per shoot day → club + age-group matching against the CRM → `var/game_opportunities.json` + DB snapshot → `POST /api/opportunities/sync/` on Railway. Matches not present in the previous snapshot are marked **New** on the page.

### Running manually

```bash
PYTHONPATH=venv/lib/python3.12/site-packages ~/.local/bin/python3.12 manage.py research_game_opportunities [flags]
#   --use-vibe          Mistral Vibe CLI web-research pass per shoot day (recommended)
#   --use-luna / --use-muse   alternative AI providers (Codex / OpenCode)
#   --push              push to Railway after saving
#   --date YYYY-MM-DD   one shoot day;  --horizon-days N   only the next N days
#   --strict            abort without saving if any schedule source fails
#   --fail-on-partial   exit 2 (after saving/pushing) if any schedule source failed
```

Railway reads its database snapshot, populated by `POST /api/opportunities/sync/` (authenticated with `X-Sync-Secret` matching `OPPORTUNITIES_SYNC_SECRET` or the legacy `PIPEDRIVE_WEBHOOK_SECRET`). Uploads fail closed when neither secret is configured.

### Sources and how they are fetched (verified 2026-11-10)

| Source | Method |
|---|---|
| AYHL | WordPress REST API `atlantichockey.org/wp-json/ayhl/v1/schedule?season=&league=` (the old `full_schedule.php` was removed in the Oct 2026 site rebuild) |
| AHF / THF | BlackBear TimeToScore signed `get_schedule_lite` API (key read from the AHF schedule page) |
| GameSheet / FedHockey | Cloudflare now JS-challenges every request, so `scheduler/public_schedule_worker.py` runs headless Chromium (Playwright under `/usr/bin/python3`), one fresh browser context per season, one date-range API call each |
| Hollydell | DaySmart public events API |
| Mistral Vibe | `vibe --prompt-file … --agent rink-research --output json` per shoot day |

### Reliability model

- **Each source is isolated.** One site failing no longer discards the others: the report is saved with a red "Partial refresh" banner and a per-day warning naming the missing source, so a failed source is never mistaken for "no games". If *every* source fails (network down) or Pipedrive fails, nothing is saved. `--strict` restores all-or-nothing behaviour.
- GameSheet: a single retired/blocked season is a warning; the source fails only when half or more of its seasons fail. AYHL refuses truncated API responses.
- HTTP calls retry with backoff on timeouts/429/5xx. Pipedrive errors never print the request URL (it carries the API token).
- **Unmapped rinks**: a shoot address that `canonical_rink` does not recognise is registered for that run by its facility name (plus any parenthetical name and a 2-word core, e.g. "Clark Gillies Arena (Dix Hills Ice Rink)" → "dix hills"), with the region taken from the address's state. League venues with the same name then match. Add a static alias in `canonical_rink` if a league spells a rink differently.
- **Vibe pass** (`scheduler/vibe_research.py`): the managed agent profile `~/.vibe/agents/rink-research.toml` disables every local tool (shell, process, file read/write/edit, subagents, MCP) so only `connector_web_search.web_search` / `open_url` can run; this was verified from the Vibe session journal (`tool_denied` for bash and read_file). Only public rink/game data is sent — no CRM names, notes, IDs or credentials; the subprocess env is an allowlist. Games are kept only with the right date/rink, an https source **that Vibe actually opened in that session**, and a quoted excerpt. A failed/timed-out day becomes a coverage note; it never discards schedule results. Vibe reads its Mistral key from the desktop keyring; if the keyring is locked (nobody logged in), put `MISTRAL_API_KEY=` in `~/.config/game-opportunities/env` (chmod 600).
- Coverage is not exhaustive: GameSheet seasons are a maintained list (`GAMESHEET_SEASONS`), and club rules (`CLUB_RULES`) are hand-curated. Club/age matches are candidates, not verified squad/level matches.
- CRM indexing prioritises one deal/lead per person, excludes archived leads and standalone persons without notes, and does not search custom fields.
- Snapshots contain CRM/customer information. They live in ignored `var/` with private file permissions. The old tracked snapshot is removed from the current source tree, but this does **not** erase the already-public Git history; coordinate any history rewrite separately.

## 10. Notes / Gotchas

- **Time zone**: Django is set to `America/New_York`. Change in `settings.py` if needed.
- **Refresh tokens** can be invalidated if Google account password changes. Re-run section 5 if so.
- **APScheduler jobs** persist in the database (via django-apscheduler) so they survive restarts.
- **Static files**: handled by whitenoise. Run `python manage.py collectstatic` before deploy
  (Railway does this automatically via the buildpack).
