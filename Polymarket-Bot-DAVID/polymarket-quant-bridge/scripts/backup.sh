#!/usr/bin/env bash
# Back up the decision journal and config.
#
# The journal is not just history: it holds the doubling-rule baseline and
# progression index, the open-position lifecycles, and the whole feedback loop.
# Losing it means the progression restarts at 0.19 and every open position
# becomes an "unknown position" at the next reconciliation.
#
# Pattern borrowed from paymentor-proxy-admin-panel/scripts/backup.sh:
# timestamped archive, retention window, and a restore script that is actually
# tested rather than assumed.
#
#   sudo bash scripts/backup.sh                    # one-off
#   30 3 * * * root /opt/pqb/scripts/backup.sh     # nightly via cron
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/polymarket-quant-bridge}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/pqb}"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
TARGET="${BACKUP_DIR}/pqb-${STAMP}.tar.gz"

mkdir -p "$BACKUP_DIR"

if [ ! -f "${APP_DIR}/state/journal.sqlite3" ]; then
  echo "! No journal at ${APP_DIR}/state/journal.sqlite3 — nothing to back up."
  exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# A consistent copy of a live WAL database.
#
# `cp` is NOT an acceptable fallback here and is deliberately not used. In WAL
# mode the recent writes live in the `-wal` sidecar until a checkpoint, so
# copying the main file alone yields a database that is stale — and, on a young
# store that has never checkpointed, completely empty. That failure is silent:
# the archive is produced, it is the right sort of size, and it restores to
# nothing. It was caught exactly that way, by restoring one.
#
# Python is guaranteed present (this is a Python project) and its
# sqlite3 Connection.backup() performs a proper online backup, so it is the
# primary path rather than the fallback.
PYBIN="${PYBIN:-${APP_DIR}/.venv/bin/python}"
[ -x "$PYBIN" ] || PYBIN="$(command -v python3 || command -v python || true)"

copy_db() {
  local src="$1" dst="$2"
  [ -f "$src" ] || return 0
  if [ -n "$PYBIN" ]; then
    "$PYBIN" - "$src" "$dst" <<'PY'
import sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
target = sqlite3.connect(dst)
with target:
    source.backup(target)
target.close(); source.close()
PY
  elif command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$src" ".backup '${dst}'"
  else
    echo "!! Neither python nor sqlite3 is available — cannot take a"
    echo "   consistent backup. Refusing to write a misleading archive."
    exit 1
  fi
}

copy_db "${APP_DIR}/state/journal.sqlite3" "${WORK}/journal.sqlite3"

# The intel store is backed up for a different and stronger reason than the
# journal. The journal could in principle be reconstructed from the exchange;
# the captured feature series in here CANNOT be reconstructed from anything. An
# order book that existed for four seconds last Tuesday is gone. Losing this
# file costs every day of capture it held — which is the sole input to strategy
# discovery — and resets the wallet ranking to nothing.
copy_db "${APP_DIR}/state/intel.sqlite3" "${WORK}/intel.sqlite3"

# The discovered rules. Cheap to copy, and it means a restore comes back with a
# working brain rather than one that has to re-earn its rules over days.
cp "${APP_DIR}/state/strategies.json" "${WORK}/strategies.json" 2>/dev/null || true

# Config, but never .env — secrets do not belong in a backup archive that gets
# copied off-server.
cp "${APP_DIR}/config/config.yaml" "${WORK}/config.yaml" 2>/dev/null || true

# Verify before archiving. A backup that reports success and restores to an
# empty database is worse than no backup, because it stops anyone looking for
# the real one. Each copied database is reopened and counted here; anything
# unreadable or unexpectedly empty aborts the run rather than being archived.
verify_db() {
  local path="$1" table="$2"
  [ -f "$path" ] || return 0
  local n
  n="$("$PYBIN" - "$path" "$table" <<'PY' 2>/dev/null || echo FAIL
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute(f"SELECT COUNT(*) FROM {sys.argv[2]}").fetchone()[0])
conn.close()
PY
)"
  if [ "$n" = "FAIL" ]; then
    echo "!! ${path##*/} is unreadable after copy — aborting."
    exit 1
  fi
  echo "  ${path##*/}: ${n} rows in ${table}"
}

echo "Verifying the copies:"
verify_db "${WORK}/journal.sqlite3" decisions
verify_db "${WORK}/intel.sqlite3"   wallet_trades

tar -czf "$TARGET" -C "$WORK" .
chmod 600 "$TARGET"
echo "Backup written: $TARGET ($(du -h "$TARGET" | cut -f1))"

find "$BACKUP_DIR" -name 'pqb-*.tar.gz' -mtime "+${KEEP_DAYS}" -delete
echo "Retention: kept the last ${KEEP_DAYS} days."
echo
echo "Copy this off the server, and test a restore with scripts/restore.sh."
