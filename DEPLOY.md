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
| `PIPEDRIVE_API_TOKEN` | Pipedrive settings -> Personal Preferences -> API | Optional |
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
2. APIs & Services -> Library -> enable **Google Calendar API**.
3. APIs & Services -> OAuth consent screen:
   - User Type: External
   - App name: "Hockey Shoot Scheduler" (or whatever)
   - Add your friend's email as a test user
   - Scopes: add `https://www.googleapis.com/auth/calendar.events`
4. Credentials -> Create credentials -> OAuth client ID:
   - Application type: Desktop app
   - Download the JSON. Copy `client_id` and `client_secret` into `.env`.
5. Run the helper script (we will build this — TBD):
   ```
   python scripts/get_refresh_token.py
   ```
   It opens a browser, your friend logs in, grants permission, and the
   script prints a `refresh_token`. Paste it into `GOOGLE_OAUTH_REFRESH_TOKEN`.

After this, the app can create calendar events forever (refresh tokens don't expire
unless revoked or unused for 6+ months).

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

(If we ever need both, we can fetch via Pipedrive API — see PIPEDRIVE_API_TOKEN.)

---

## 7. ClickUp edit tasks

The edit automation listens for Pipedrive activities whose type is exactly one of:

- `Recruiting Highlight Video`
- `Hype Video`
- `Highlight Recap`

When one arrives, the app chooses an editor using the per-video-type ranking and
that editor's `max_active_jobs` threshold. It then creates a ClickUp task in the
Editing Projects list using:

- Pipedrive activity subject -> ClickUp task title
- Pipedrive due date -> ClickUp due date (date-only)
- Pipedrive activity notes/description -> ClickUp description
- selected editor's `clickup_user_id` -> ClickUp assignee

A one-time migration seeds the initial editor rows, ClickUp user IDs, and rankings.
Django records that migration, so it will not run again on later deploys.

## 8. Notes / Gotchas

- **Time zone**: Django is set to `America/New_York`. Change in `settings.py` if needed.
- **Refresh tokens** can be invalidated if Google account password changes. Re-run section 5 if so.
- **APScheduler jobs** persist in the database (via django-apscheduler) so they survive restarts.
- **Static files**: handled by whitenoise. Run `python manage.py collectstatic` before deploy
  (Railway does this automatically via the buildpack).
