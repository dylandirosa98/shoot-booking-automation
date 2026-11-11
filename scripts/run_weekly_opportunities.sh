#!/usr/bin/env bash
# Weekly Game Opportunities refresh (run by the game-opportunities-weekly systemd
# timer every Wednesday). Pulls upcoming shoots + full CRM from Pipedrive, fetches
# AYHL / AHF / THF / GameSheet / FedHockey / Hollydell schedules, runs a Mistral
# Vibe CLI web-research pass per shoot day, matches CRM people to teams playing at
# our shoot rinks, saves locally, and pushes the report to the Railway dashboard.
#
# Manual run:  scripts/run_weekly_opportunities.sh            (full weekly run)
#              scripts/run_weekly_opportunities.sh --no-push  (local only)
#              OPPORTUNITIES_NO_VIBE=1 scripts/run_weekly_opportunities.sh  (skip AI pass)
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$APP_DIR/var/logs"
mkdir -p -m 700 "$LOG_DIR"
LOG_FILE="$LOG_DIR/opportunities-$(date +%Y-%m-%d_%H%M).log"

# Vibe reads its Mistral API key from the desktop keyring over the session bus.
# An optional env file can supply MISTRAL_API_KEY instead (for when the keyring is locked).
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"
ENV_FILE="${OPPORTUNITIES_ENV_FILE:-$HOME/.config/game-opportunities/env}"
if [[ -f "$ENV_FILE" ]]; then set -a; source "$ENV_FILE"; set +a; fi

PYTHON="${OPPORTUNITIES_PYTHON:-$HOME/.local/bin/python3.12}"
export PYTHONPATH="$APP_DIR/venv/lib/python3.12/site-packages"
export PYTHONUNBUFFERED=1

ARGS=(research_game_opportunities --fail-on-partial)
[[ "${OPPORTUNITIES_NO_VIBE:-0}" == "1" ]] || ARGS+=(--use-vibe)
if [[ " $* " != *" --no-push "* ]]; then ARGS+=(--push); fi
[[ -n "${OPPORTUNITIES_HORIZON_DAYS:-}" ]] && ARGS+=(--horizon-days "$OPPORTUNITIES_HORIZON_DAYS")

cd "$APP_DIR"
umask 077
# One run at a time; a second invocation exits immediately instead of piling up.
exec 9>"$APP_DIR/var/.opportunities.lock"
if ! flock -n 9; then
  echo "Another Game Opportunities refresh is already running; exiting." | tee -a "$LOG_FILE"
  exit 0
fi

echo "=== Game Opportunities weekly refresh $(date -Is) ===" | tee -a "$LOG_FILE"
"$PYTHON" manage.py "${ARGS[@]}" 2>&1 | tee -a "$LOG_FILE"
status=${PIPESTATUS[0]}
echo "=== finished $(date -Is) exit=$status ===" | tee -a "$LOG_FILE"

# Keep the last 26 weekly logs (~6 months).
ls -1t "$LOG_DIR"/opportunities-*.log 2>/dev/null | tail -n +27 | xargs -r rm -f
exit "$status"
