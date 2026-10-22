#!/usr/bin/env bash
# Daily job: sync the last 3 days (overwriting, so late-synced data fills in), then optionally
# fill daily_summary.csv (HSYNC_UPDATE_CSV=1). Safe to run repeatedly; a lock prevents overlapping runs.
# Settings: environment, else hsync.local.env next to this script (or $HSYNC_LOCAL_ENV), else defaults.
# Exit code: 0 ok, 2 re-auth needed, 3 partial (some data groups failed), 4 already running, 1 error.
set -euo pipefail
umask 077
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Same rules as health_sync/config.py:load_local_env: KEY=VALUE lines, HSYNC_* keys only, nothing is
# expanded or executed, the environment wins, a group/world-writable file is ignored.
load_local_env() {
  local file="${HSYNC_LOCAL_ENV-$DIR/hsync.local.env}" line key val n=0
  [ -n "$file" ] && [ -f "$file" ] || return 0
  if [ -n "$(find "$file" -maxdepth 0 -perm /022 2>/dev/null)" ]; then
    echo "run_sync: ignoring $file: group/world-writable (chmod 600 it)" >&2
    return 0
  fi
  while IFS= read -r line || [ -n "$line" ]; do
    n=$((n + 1))
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    case "$line" in ''|'#'*) continue ;; esac
    case "$line" in 'export '*) line="${line#export }"; line="${line#"${line%%[![:space:]]*}"}" ;; esac
    key="${line%%=*}"
    val="${line#*=}"
    if [ "$key" = "$line" ] || [[ ! "$key" =~ ^HSYNC_[A-Z0-9_]+$ ]] || [ "$key" = "HSYNC_LOCAL_ENV" ]; then
      echo "run_sync: $file:$n: ignored (expected HSYNC_NAME=value)" >&2
      continue
    fi
    if [ "${#val}" -ge 2 ] && { { [ "${val:0:1}" = '"' ] && [ "${val: -1}" = '"' ]; } || { [ "${val:0:1}" = "'" ] && [ "${val: -1}" = "'" ]; }; }; then
      val="${val:1:${#val}-2}"
    fi
    [ -n "${!key+x}" ] && continue
    export "$key=$val"
  done < "$file"
}
load_local_env

LOG="$DIR/sync.log"
LOG_MAX_BYTES=5000000          # keep the log bounded (~5 MB, one rotation)
DAYS="${HSYNC_DAYS:-3}"
UPDATE_CSV="${HSYNC_UPDATE_CSV:-0}"
RUN_TIMEOUT="${HSYNC_RUN_TIMEOUT:-1800}"   # hard cap for one sync (seconds)
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

cd "$DIR" || exit 1

# validate env input before it reaches any command line
case "$DAYS" in ''|*[!0-9]*) echo "run_sync: HSYNC_DAYS must be a positive integer" >&2; exit 1 ;; esac
case "$RUN_TIMEOUT" in ''|*[!0-9]*) echo "run_sync: HSYNC_RUN_TIMEOUT must be a positive integer" >&2; exit 1 ;; esac

if [ -f "$LOG" ] && [ ! -L "$LOG" ]; then
  size="$(stat -c %s -- "$LOG" 2>/dev/null || echo 0)"
  if [ "$size" -gt "$LOG_MAX_BYTES" ]; then mv -f -- "$LOG" "$LOG.1"; fi
fi

{
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') run_sync start (days=$DAYS)"
  rc=0
  timeout --kill-after=30 "$RUN_TIMEOUT" "$DIR/hsync" sync --days "$DAYS" || rc=$?
  case "$rc" in
    0|1|2|3|4) ;;
    124|137) echo "ERROR: sync timed out after ${RUN_TIMEOUT}s"; rc=1 ;;
    *) echo "ERROR: sync exited with unexpected code $rc"; rc=1 ;;
  esac
  if [ "$UPDATE_CSV" = "1" ] && { [ "$rc" -eq 0 ] || [ "$rc" -eq 3 ]; }; then
    "$DIR/.venv/bin/python" "$DIR/update_daily_summary.py" || echo "WARN: daily_summary.csv update failed"
  fi
  echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') run_sync end rc=$rc"
  exit "$rc"
} >>"$LOG" 2>&1
