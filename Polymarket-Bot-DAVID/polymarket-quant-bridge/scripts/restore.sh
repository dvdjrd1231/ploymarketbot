#!/usr/bin/env bash
# Restore a journal backup produced by scripts/backup.sh.
#
#   sudo bash scripts/restore.sh /var/backups/pqb/pqb-20260810-030000.tar.gz
#
# A backup you have never restored is a guess, not a backup. Test this against
# a scratch directory before you need it:
#   APP_DIR=/tmp/pqb-restore-test bash scripts/restore.sh <archive>
set -euo pipefail

ARCHIVE="${1:-}"
APP_DIR="${APP_DIR:-/opt/polymarket-quant-bridge}"

if [ -z "$ARCHIVE" ] || [ ! -f "$ARCHIVE" ]; then
  echo "usage: restore.sh <archive.tar.gz>"
  exit 1
fi

echo "This will replace ${APP_DIR}/state/journal.sqlite3"
echo "  with the copy inside ${ARCHIVE}"
echo
echo "The journal holds the doubling-rule baseline and progression index, so"
echo "restoring an older one rewinds the trade-size progression to whatever it"
echo "was at that moment. Stop the service first."
read -r -p "Continue? [y/N] " reply
[ "$reply" = "y" ] || { echo "Aborted."; exit 1; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
tar -xzf "$ARCHIVE" -C "$WORK"

mkdir -p "${APP_DIR}/state"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

restore_db() {
  local name="$1"
  [ -f "${WORK}/${name}" ] || { echo "  (${name} not in this archive — skipped)"; return 0; }
  if [ -f "${APP_DIR}/state/${name}" ]; then
    mv "${APP_DIR}/state/${name}" "${APP_DIR}/state/${name}.replaced-${STAMP}"
  fi
  # WAL sidecars from the old database would be applied on top of the restored
  # one and corrupt it.
  rm -f "${APP_DIR}/state/${name}-wal" "${APP_DIR}/state/${name}-shm"
  cp "${WORK}/${name}" "${APP_DIR}/state/${name}"
  echo "  restored ${name}"
}

restore_db journal.sqlite3
restore_db intel.sqlite3
if [ -f "${WORK}/strategies.json" ]; then
  cp "${WORK}/strategies.json" "${APP_DIR}/state/strategies.json"
  echo "  restored strategies.json"
fi
echo "Anything replaced was kept alongside, suffixed .replaced-${STAMP}."

echo
echo "Restored. Verify BEFORE starting the service:"
echo "  python -m pqb.cli status     # doubling baseline, index, open positions"
echo "  python -m pqb.cli wallets    # the ranking came back with the intel store"
echo
echo "The first reconciliation after restart compares this journal against the"
echo "live exchange. If it is out of date it will halt — which is correct."
