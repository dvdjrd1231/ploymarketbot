#!/usr/bin/env bash
#
# Is the bridge actually working — not merely running?
#
#   bash scripts/healthcheck.sh          # human-readable
#   bash scripts/healthcheck.sh --quiet  # exit code only, for cron/monitoring
#
# Exit codes:  0 healthy   1 degraded (warnings)   2 unhealthy (failures)
#
# A process can be "active (running)" while doing nothing useful: the universe
# empty, the clock drifted, the disk full, or a reconciliation halt engaged
# hours ago that nobody noticed. Each check below is one of those.
set -uo pipefail

APP_DIR="${APP_DIR:-/opt/polymarket-quant-bridge}"
CONFIG="${CONFIG:-${APP_DIR}/config/config.yaml}"
# The native install puts dependencies in a venv; the container installs them
# system-wide and has no venv at all. Falling through to the interpreter on PATH
# keeps one script correct in both, instead of silently reporting "?" for the
# strategy count inside a container.
PY="${PY:-${APP_DIR}/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python || echo python3)"

# Query a SQLite database, returning the first row space-separated.
#
# The sqlite3 CLI is NOT assumed. Both deployment paths install it, but a host
# that lacks it used to make every query return empty — which this script then
# read as "no cycles, no wallets" and reported as UNHEALTHY for a perfectly
# healthy bridge. Under Docker that verdict drives restarts, so a missing CLI
# could restart-loop a working system. Python is guaranteed present, so it is
# the fallback; if neither works, `sq` returns the sentinel `__NOQUERY__` and
# the caller warns instead of inventing a failure.
sq() {
  local db="$1" sql="$2" out
  [ -f "$db" ] || { echo "__NOQUERY__"; return; }
  if command -v sqlite3 >/dev/null 2>&1; then
    out="$(sqlite3 -separator ' ' "$db" "$sql" 2>/dev/null)" && \
      { echo "$out"; return; }
  fi
  out="$("$PY" - "$db" "$sql" <<'PY' 2>/dev/null
import sqlite3, sys
conn = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
row = conn.execute(sys.argv[2]).fetchone() or ()
print(" ".join("" if v is None else str(v) for v in row))
conn.close()
PY
)" && { echo "$out"; return; }
  echo "__NOQUERY__"
}
STATE="${APP_DIR}/state"
QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

FAIL=0; WARN=0
ok()   { [ "$QUIET" -eq 1 ] || printf '  \033[1;32mok\033[0m    %s\n' "$*"; }
warn() { WARN=1; [ "$QUIET" -eq 1 ] || printf '  \033[1;33mwarn\033[0m  %s\n' "$*"; }
bad()  { FAIL=1; [ "$QUIET" -eq 1 ] || printf '  \033[1;31mFAIL\033[0m  %s\n' "$*"; }
head() { [ "$QUIET" -eq 1 ] || printf '\n\033[1m%s\033[0m\n' "$*"; }

# In a container there is no systemd and the process is PID 1 — its liveness is
# the container runtime's business, not ours. Checking `systemctl` there would
# report the service dead on a perfectly healthy container, so the process check
# is skipped and the evidence-based checks below carry the verdict.
IN_CONTAINER=0
if [ -f /.dockerenv ] || grep -qE '(docker|containerd|kubepods)' /proc/1/cgroup 2>/dev/null
then
  IN_CONTAINER=1
fi

head "process"
if [ "$IN_CONTAINER" -eq 1 ]; then
  ok "running in a container (liveness is the runtime's job)"
elif systemctl is-active --quiet pqb 2>/dev/null; then
  since="$(systemctl show pqb -p ActiveEnterTimestamp --value 2>/dev/null)"
  ok "pqb service active (since ${since:-unknown})"
  restarts="$(systemctl show pqb -p NRestarts --value 2>/dev/null || echo 0)"
  # Restarts are the tell for a crash loop that Restart=always is papering over.
  [ "${restarts:-0}" -gt 5 ] && warn "pqb has restarted ${restarts} times"
else
  bad "pqb service is not active"
fi

head "trading gate"
if [ -f "${STATE}/HALT" ]; then
  bad "RECONCILIATION HALT engaged — the exchange and the bridge disagree."
  [ "$QUIET" -eq 1 ] || sed -n '1,12p' "${STATE}/HALT" | sed 's/^/         /'
  [ "$QUIET" -eq 1 ] || echo "         review, then: pqb.cli resume --force"
else
  ok "no reconciliation halt"
fi
if [ -f "${STATE}/KILL" ]; then
  warn "kill switch ARMED ($(tr -d '\n' < "${STATE}/KILL")) — no orders are being sent"
else
  ok "kill switch clear"
fi

head "recent activity"
if [ -f "${STATE}/journal.sqlite3" ]; then
  # A cycle every 20s by default; nothing for 10 minutes means the loop is
  # wedged even if the process still looks alive.
  age="$(sq "${STATE}/journal.sqlite3" \
        "SELECT CAST(strftime('%s','now') - MAX(ts) AS INT) FROM cycles")"
  if [ "$age" = "__NOQUERY__" ]; then
    warn "cannot read the journal (no sqlite3 CLI and no working python)"
  elif [ -z "$age" ]; then
    warn "no cycles recorded yet"
  elif [ "$age" -gt 600 ]; then
    bad "last cycle was ${age}s ago — the loop is not turning"
  else
    ok "last cycle ${age}s ago"
  fi
  errs="$(sq "${STATE}/journal.sqlite3" \
        "SELECT COALESCE(SUM(errors),0) FROM cycles WHERE ts > strftime('%s','now')-3600")"
  case "$errs" in
    __NOQUERY__|"") : ;;
    *) [ "$errs" -gt 20 ] && warn "${errs} cycle errors in the last hour" \
                          || ok "${errs} cycle errors in the last hour" ;;
  esac
else
  warn "no journal yet at ${STATE}/journal.sqlite3"
fi

head "analytical layer"
if [ -f "${STATE}/intel.sqlite3" ]; then
  counts="$(sq "${STATE}/intel.sqlite3" \
    "SELECT (SELECT COUNT(DISTINCT wallet) FROM wallet_trades),
            (SELECT COUNT(*) FROM wallet_scores WHERE rank>0),
            (SELECT COUNT(*) FROM research_rows)")"
  if [ "$counts" = "__NOQUERY__" ]; then
    warn "cannot read the intel store — not treating that as a failure"
  else
    read -r wallets ranked rows <<<"$counts"
    [ "${wallets:-0}" -gt 0 ] && ok "${wallets} wallets observed, ${ranked} ranked" \
                              || bad "no wallets observed — ingestion is not running"
    # Discovery needs ~200 rows per token; report progress toward that honestly.
    [ "${rows:-0}" -gt 0 ] && ok "${rows} captured feature rows" \
                           || warn "no feature rows captured — research will have no input"
  fi
else
  warn "no intel store yet at ${STATE}/intel.sqlite3"
fi

head "strategies"
if [ -f "${STATE}/strategies.json" ]; then
  n="$("$PY" -c "import json,sys;print(len(json.load(open(sys.argv[1]))['strategies']))" \
       "${STATE}/strategies.json" 2>/dev/null || echo "?")"
  age_d=$(( ( $(date +%s) - $(stat -c %Y "${STATE}/strategies.json") ) / 86400 ))
  ok "${n} discovered rule(s), refreshed ${age_d}d ago"
  [ "${age_d:-0}" -gt 7 ] && warn "strategies are stale — is pqb-research.timer running?"
else
  warn "no strategies yet — the engine is using baseline scoring (run: systemctl start pqb-research)"
fi

head "host"
avail_kb="$(df -Pk "$STATE" | awk 'NR==2{print $4}')"
avail_mb=$(( avail_kb / 1024 ))
if [ "$avail_mb" -lt 500 ]; then
  bad "only ${avail_mb} MB free on the state filesystem"
elif [ "$avail_mb" -lt 2048 ]; then
  warn "${avail_mb} MB free on the state filesystem"
else
  ok "${avail_mb} MB free"
fi

# A drifting clock silently mis-ages every quote and wallet observation.
# A container inherits the host's clock, so there is nothing to check inside
# one — sync the HOST.
if [ "$IN_CONTAINER" -eq 1 ]; then
  ok "clock is the host's (ensure the HOST is NTP-synced)"
elif command -v timedatectl >/dev/null 2>&1; then
  if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
    ok "clock synchronised"
  else
    warn "clock is NOT NTP-synchronised"
  fi
fi

if [ "$IN_CONTAINER" -eq 0 ] && [ -d /var/backups/pqb ]; then
  newest="$(find /var/backups/pqb -name 'pqb-*.tar.gz' -printf '%T@\n' 2>/dev/null | sort -rn | head -1)"
  if [ -n "$newest" ]; then
    days=$(( ( $(date +%s) - ${newest%.*} ) / 86400 ))
    [ "$days" -gt 2 ] && warn "newest backup is ${days}d old" || ok "backup ${days}d old"
  else
    warn "no backups found in /var/backups/pqb"
  fi
fi

if [ "$QUIET" -eq 0 ]; then
  echo
  if [ "$FAIL" -eq 1 ]; then echo "VERDICT: UNHEALTHY"
  elif [ "$WARN" -eq 1 ]; then echo "VERDICT: DEGRADED"
  else echo "VERDICT: HEALTHY"; fi
fi

[ "$FAIL" -eq 1 ] && exit 2
[ "$WARN" -eq 1 ] && exit 1
exit 0
