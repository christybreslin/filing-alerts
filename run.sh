#!/usr/bin/env bash
# Cron wrapper for the crypto staking-ETF filing pipeline.
# - single-instance lock (skips if a prior run is still going)
# - loads secrets from .env, runs in the project venv
# - logs to run.log; on failure posts a loud alert to Slack
# - optional dead-man's-switch ping (HEALTHCHECK_URL) so silence is detectable (FR-8)
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
LOG="$DIR/run.log"
ts() { date -u +%FT%TZ; }

# --- single instance ---
exec 9>"$DIR/.run.lock"
if ! flock -n 9; then
  echo "$(ts) skip: previous run still active" >> "$LOG"
  exit 0
fi

# --- secrets ---
set -a; . "$DIR/.env"; set +a
DAYS="${RUN_DAYS:-3}"

echo "$(ts) start (--days $DAYS)" >> "$LOG"
if "$DIR/.venv/bin/python" run.py --days "$DAYS" >> "$LOG" 2>&1; then
  echo "$(ts) ok" >> "$LOG"
  [ -n "${HEALTHCHECK_URL:-}" ] && curl -fsS -m 10 "$HEALTHCHECK_URL" >/dev/null 2>&1 || true
else
  export FAIL_CODE=$?
  echo "$(ts) FAILED exit=$FAIL_CODE" >> "$LOG"
  # Fail loudly — post to Slack so a broken run is visible, not silent.
  "$DIR/.venv/bin/python" - <<'PY' >> "$LOG" 2>&1 || true
import os, slack_post
ch = os.environ.get("HEALTH_CHANNEL", slack_post.DEFAULT_CHANNEL)
slack_post.post(ch, f":warning: SEC filing pipeline run FAILED (exit {os.environ.get('FAIL_CODE')}). "
                    f"Check run.log on the server.")
PY
  [ -n "${HEALTHCHECK_URL:-}" ] && curl -fsS -m 10 "${HEALTHCHECK_URL}/fail" >/dev/null 2>&1 || true
  exit "$FAIL_CODE"
fi
