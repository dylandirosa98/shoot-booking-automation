# Shoot SMS reminders

## Ownership and launch state

This feature extends the existing shoot scheduler, not the sales cadence. Pipedrive activities own shoot time/location/deletion; the scheduling app owns the **confirmed videographer assignment**. CRM deal owners and customer phone numbers are never used as videographer recipients.

The feature and live sends default OFF independently. No real videographer texts, CRM edits, subscriptions, deployment, or Jack-account setup are performed by installing the code.

## Flow

1. Existing `/webhook/pipedrive/` activity create/change/delete subscriptions wake the app. With `SHOOT_REMINDERS_ENABLED=true`, fetch the current activity by exact activity ID instead of trusting possibly delayed webhook fields. CRM read failure returns HTTP 503 for retry. Never cancel all shoots by deal ID.
2. Changes to a confirmed Shoot or its assigned Videographer update the reminder ledger. Schedule 48h, 24h, 3h before its absolute UTC start. Render local game time using America/New_York with timezone label.
3. Cancel pending reminders on cancellation/deletion/reassignment. Already-sent milestones are not resent when the same assignment's time changes. Skip elapsed milestones for late bookings rather than blasting missed texts.
4. A one-minute worker re-fetches the source activity and rechecks the assignment immediately before a due send. A separate periodic reconciliation repairs missed webhook updates/deletions and surfaces missed creates as pending, without sending invites or inventing assignments.
5. Claim the ledger row atomically before the network call. Persist provider evidence on success. Ambiguous POST/readback results are manual-review states, never an automatic resend. A worker crash after claiming must likewise not resend blindly.

## Jack's messaging bridge — required before live activation

Use Jack's Mac and iPhone, signed into his Apple account. Enable carrier Text Message Forwarding to the Mac, keep both online, and install/configure BlueBubbles with Private API for availability checks. The worker needs an authenticated private-network/tunnel connection to that Mac; a Railway process cannot access a home LAN address directly. Do not expose an unauthenticated public port or reuse Dylan's bridge credentials.

Use only dedicated `SHOOT_REMINDERS_BLUEBUBBLES_*` settings. Configure Jack's E.164 number as EXPECTED. Prove iMessage and a brand-new non-iMessage destination with Dylan's approved test number **8563057016** only; verify the recipient actually sees Jack's number and inspect the native Messages record. Only after that proof set VERIFIED to the same number and SENDER_VERIFIED=true. A config assertion is not independent proof of sender identity.

The adapter selects an exact one-to-one existing chat, otherwise explicitly selects iMessage only on positive availability; failures/unavailable default to SMS. It verifies provider persistence by a separate read. Persistence is **not a guarantee of recipient delivery**. Never auto-retry uncertain sends.

## Deployment sequence

- Install dependencies in a compatible Python environment (tested environment: `.venv-reminders`).
- Run full tests and `manage.py makemigrations --check --dry-run`.
- Back up the destination DB, then run `manage.py migrate` before enabling the feature.
- Populate valid videographer phone numbers in the scheduling roster; missing/invalid/inactive recipients stay blocked.
- Verify a working Pipedrive token privately. The original local scheduling token was HTTP 401 during implementation; the separate sales connection was HTTP 200. A dedicated reminder token override is supported without changing Facebook/sales credentials.
- Configure the feature flag, keep SEND_ENABLED=false, run reconciliation and inspect dry-run counts.
- Complete the Jack-number bridge tests above; only then enable SEND_ENABLED.
- Run a dedicated worker with `python manage.py run_shoot_reminders --loop` (one-minute polling and 15-minute reconciliation). This command deliberately does not start invite/escalation jobs.
- For a one-shot repair: `python manage.py run_shoot_reminders --reconcile`.
- For read-only inspection: `python manage.py run_shoot_reminders --dry-run`; it performs no ledger claims or reconciliation writes. With the feature disabled it prints `{"status": "disabled"}`.
- For operator health and manual-review counts: `python manage.py run_shoot_reminders --status`; this makes no network calls and works with sends disabled (after migration).
- Follow manual-review records after ambiguous sends; never reset them to pending without checking native Messages first.

## Configuration

See the Shoot SMS reminders section of `.env.example`. Store values in gitignored `.env` or deployment secrets, never chat or source control. Keep normal operational security controls intact.

## Known integration limits

- Pipedrive does not identify the confirmed videographer by activity owner. New activities discovered after missed create webhooks are deliberately pending until a real assignment is confirmed.
- Local scheduling DB and production DB are distinct. Local roster checks do not establish the completeness of the production roster.
- No live rollout can be claimed until the deployed migration, worker, Pipedrive reads, assignment/phone data, private bridge network path, and recipient-visible sender proof all pass.
