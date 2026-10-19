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

### Running locally & syncing to Railway

```bash
# Run locally across upcoming shoot days and save privately to var/game_opportunities.json + DB
python manage.py research_game_opportunities

# Require the local Muse Spark web research pass to succeed
python manage.py research_game_opportunities --use-muse

# Push only after successful schedule retrieval and Muse research
python manage.py research_game_opportunities --use-muse --push

# Recommended: Luna via existing Codex/ChatGPT login, bypassing expired Muse credentials.
# This machine's verified GameSheet-compatible public HTTP runtime:
PYTHONPATH=venv/lib/python3.12/site-packages \
  /home/dylandirosa/.local/bin/python3.12 manage.py research_game_opportunities \
  --use-luna --public-python /usr/bin/python3 --push
```

Railway reads its database snapshot, populated by `POST /api/opportunities/sync/` (authenticated with `X-Sync-Secret` matching `OPPORTUNITIES_SYNC_SECRET` or the legacy `PIPEDRIVE_WEBHOOK_SECRET`). Uploads fail closed when neither secret is configured. The publisher and server must share the same secret. Snapshots are no longer bundled in deployments; preserve the production database and verify a snapshot exists before deploying this change.

### Refresh safety and known limitations

- Schedule HTTP errors, unrecognized responses, detected truncation, and Muse failures now abort **before saving or pushing**. They must not be interpreted as zero games. This does not roll back a snapshot previously published by older code.
- October 18 investigation: the Python 3.12/requests environment returned HTTP 403 from GameSheet, while system Python 3.10/requests returned HTTP 200 for the same public schedule request. `--public-python` (or `OPPORTUNITIES_PUBLIC_PYTHON`) runs only public GameSheet requests in that compatible runtime, with a fixed endpoint and no CRM credentials or inherited `PYTHONPATH`. Failed requests still abort; TLS verification remains enabled.
- The AYHL parser ignores HTML comments, including a deprecated date cell that otherwise shifts the live date/time column.
- Muse requires a valid OpenCode provider API key. The CLI reported `api_key_expired` during the investigation; replace that key in OpenCode Console/client configuration before trying again. Do not paste the key into this repository.
- `--use-luna` instead uses `codex exec --model gpt-6-luna` with the existing Codex/ChatGPT account. Luna authentication, browser navigation, page snapshots and a complete single-day structured result were verified. Each shoot day must return a validated result and actual successful browser navigation + snapshot events; failed/skipped AI passes abort the refresh. Up to three days run concurrently. Only public rink/game records go to the worker, not CRM information. Coverage limitations appear on the page, and no model can guarantee that every unpublished game is found.
- The Muse worker uses its own working directory/session context, denies shell/file modification tools and browser code execution, and has websearch/webfetch plus an isolated, headless Playwright MCP browser (`@playwright/mcp@0.0.83`, launched with `npx`). Navigation and snapshots were smoke-tested against AYHL independently of the model; successful Muse execution is still blocked by the expired key. Set `OPPORTUNITIES_BROWSER_EXECUTABLE` if automatic Chromium discovery cannot find a browser. Node/npm and an installed Chromium are local prerequisites, not Railway dependencies.
- Coverage is not exhaustive: GameSheet seasons/regions and rink aliases are a maintained list. Essex Sports Center, Icenter Salem, Rockland Skating Rink, and Hobomock Arena now have aliases. Great American Center is a roller-hockey facility and is explicitly flagged outside the five ice-league sources instead of implying there are no games.
- CRM indexing currently prioritizes one deal/lead per person, excludes archived leads and standalone persons without notes, and does not search all custom fields. Club/age matches are candidates, not verified squad/level matches.
- Snapshots contain CRM/customer information. They live in ignored `var/` with private file permissions. The old tracked snapshot is removed from the current source tree, but this does **not** erase the already-public Git history. Historical privacy remediation must be coordinated separately; do not force-push rewritten history without explicit approval.

## 10. Notes / Gotchas

- **Time zone**: Django is set to `America/New_York`. Change in `settings.py` if needed.
- **Refresh tokens** can be invalidated if Google account password changes. Re-run section 5 if so.
- **APScheduler jobs** persist in the database (via django-apscheduler) so they survive restarts.
- **Static files**: handled by whitenoise. Run `python manage.py collectstatic` before deploy
  (Railway does this automatically via the buildpack).
