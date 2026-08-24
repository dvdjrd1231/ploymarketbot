#!/usr/bin/env bash
#
# One-shot installer for a fresh Debian 12/13 or Ubuntu 22.04/24.04 VPS.
#
#   sudo bash deploy/install.sh
#
# What it does, and why each step is here:
#
#   1. Installs system packages (python3-venv, sqlite3, git, tzdata, chrony).
#   2. Creates a dedicated unprivileged `pqb` user that owns nothing else.
#   3. Lays the three repositories out where the path shims expect them.
#   4. Builds ONE virtualenv holding all three projects' dependencies —
#      this project, ploymarketbot's clients, and the Quant Bridge's pandas /
#      numpy. Two interpreters cannot work: `pqb.cli run` needs the Polymarket
#      clients AND the bridge's feature engineering in the same process.
#   5. Creates /etc/pqb/pqb.env chmod 600 for the private key.
#   6. Installs the systemd units and timers.
#
# It is idempotent: re-running upgrades in place and never overwrites an
# existing config, .env, or state directory.
#
# It does NOT start trading. It leaves the service installed but stopped, in
# dry-run, so the operator runs `pqb.cli check` and reviews before arming.
set -euo pipefail

APP_USER="${APP_USER:-pqb}"
APP_DIR="${APP_DIR:-/opt/polymarket-quant-bridge}"
# The two sibling projects the path shims resolve. Kept beside APP_DIR by
# default so the shims' defaults work with no environment overrides at all.
PARENT_DIR="$(dirname "$APP_DIR")"
UPSTREAM_DIR="${UPSTREAM_DIR:-${PARENT_DIR}/ploymarketbot}"
BRIDGE_DIR="${BRIDGE_DIR:-${PARENT_DIR}/qc_lean_bridge}"
ENV_DIR="${ENV_DIR:-/etc/pqb}"
VENV="${APP_DIR}/.venv"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m !  %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m !! %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root: sudo bash deploy/install.sh"

# ---------------------------------------------------------------- 1. packages
say "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq \
  python3 python3-venv python3-dev build-essential \
  sqlite3 git curl ca-certificates tzdata chrony >/dev/null
# A trading process compares exchange timestamps with its own clock; a drifting
# VPS clock silently mis-ages every quote and every wallet observation.
systemctl enable --now chrony >/dev/null 2>&1 || \
  systemctl enable --now systemd-timesyncd >/dev/null 2>&1 || \
  warn "No time-sync daemon started — check the clock manually."

PYVER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
python3 - <<'PY' || die "Python 3.10+ is required."
import sys
raise SystemExit(0 if sys.version_info >= (3, 10) else 1)
PY
say "Python ${PYVER} OK"

# ------------------------------------------------------------------- 2. user
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  say "Creating service user '${APP_USER}'"
  useradd --system --create-home --home-dir "/home/${APP_USER}" \
          --shell /usr/sbin/nologin "$APP_USER"
else
  say "Service user '${APP_USER}' already exists"
fi

# --------------------------------------------------------------- 3. layout
say "Checking the three repositories"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$SRC_DIR" != "$APP_DIR" ]; then
  say "Copying this project to ${APP_DIR}"
  mkdir -p "$APP_DIR"
  # state/ and config/ are preserved: a re-run must never discard the journal
  # (it holds the doubling baseline and every open position) or the config.
  tar -C "$SRC_DIR" \
      --exclude=.venv --exclude=state --exclude=__pycache__ \
      --exclude=.pytest_cache --exclude='*.pyc' \
      -cf - . | tar -C "$APP_DIR" -xf -
fi

missing=0
if [ ! -f "${UPSTREAM_DIR}/backend/services/trading.py" ]; then
  warn "ploymarketbot NOT found at ${UPSTREAM_DIR}"
  warn "  It provides the Polymarket clients. Copy or clone it there, e.g.:"
  warn "    rsync -a ploymarketbot/ ${APP_USER}@<host>:${UPSTREAM_DIR}/"
  missing=1
fi
if [ ! -f "${BRIDGE_DIR}/core/pipeline.py" ]; then
  warn "qc_lean_bridge NOT found at ${BRIDGE_DIR}"
  warn "  It is the decision layer. Copy the qc_lean_bridge folder there:"
  warn "    rsync -a 'Advanced Strategy Development for QuantConnect LEAN/qc_lean_bridge/' \\"
  warn "          ${APP_USER}@<host>:${BRIDGE_DIR}/"
  missing=1
fi

# ------------------------------------------------------------------ 4. venv
say "Building the virtualenv at ${VENV}"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install --quiet --upgrade pip wheel

say "Installing this project's requirements (includes pandas/numpy for the bridge)"
"$VENV/bin/python" -m pip install --quiet -r "${APP_DIR}/requirements.txt"

if [ -f "${UPSTREAM_DIR}/requirements.txt" ]; then
  say "Installing ploymarketbot's requirements into the SAME venv"
  # Deliberately one environment: `pqb.cli run` needs the Polymarket clients
  # and the bridge's feature engineering inside one process.
  "$VENV/bin/python" -m pip install --quiet -r "${UPSTREAM_DIR}/requirements.txt" || \
    warn "Some ploymarketbot requirements failed — live trading may not work."
fi
# The bridge's own requirements.txt is NOT installed: it pins PyQt6 for its
# desktop GUI (~85 MB, needs X libs). pandas/numpy above are all it needs here.

# ------------------------------------------------------------ 5. config, env
say "Config and secrets"
mkdir -p "$ENV_DIR"
if [ ! -f "${APP_DIR}/config/config.yaml" ]; then
  cp "${APP_DIR}/config/config.example.yaml" "${APP_DIR}/config/config.yaml"
  say "  created config/config.yaml from the example (dry-run)"
else
  say "  config/config.yaml exists — left untouched"
fi

if [ ! -f "${ENV_DIR}/pqb.env" ]; then
  cat > "${ENV_DIR}/pqb.env" <<'ENVEOF'
# Secrets for the Polymarket Quant Bridge. chmod 600, owned by the pqb user.
#
# LEAVE THESE BLANK until you are ready to trade live. Dry-run needs neither.
#
# The key is read on THIS machine and nowhere else. It is never transmitted,
# logged, committed, or returned by any API path. Whoever controls this host
# controls the wallet.
PQB_PRIVATE_KEY=
PQB_FUNDER_ADDRESS=

# Where the two sibling projects live. The shims fall back to sensible
# defaults; these make it explicit and survive a relocation.
ENVEOF
  {
    echo "PQB_POLYMARKETBOT_PATH=${UPSTREAM_DIR}"
    echo "PQB_QUANT_BRIDGE_PATH=${BRIDGE_DIR}"
  } >> "${ENV_DIR}/pqb.env"
  say "  created ${ENV_DIR}/pqb.env"
else
  say "  ${ENV_DIR}/pqb.env exists — left untouched"
fi
chown -R "${APP_USER}:${APP_USER}" "$ENV_DIR"
chmod 700 "$ENV_DIR"
chmod 600 "${ENV_DIR}/pqb.env"

mkdir -p "${APP_DIR}/state" /var/backups/pqb
chown -R "${APP_USER}:${APP_USER}" "$APP_DIR" /var/backups/pqb
chmod 750 "${APP_DIR}/state"

# --------------------------------------------------------------- 6. systemd
say "Installing systemd units"
for unit in pqb.service pqb-research.service pqb-research.timer \
            pqb-backup.service pqb-backup.timer; do
  if [ -f "${APP_DIR}/deploy/${unit}" ]; then
    install -m 0644 "${APP_DIR}/deploy/${unit}" "/etc/systemd/system/${unit}"
  fi
done
systemctl daemon-reload
systemctl enable pqb-research.timer pqb-backup.timer >/dev/null 2>&1 || true
systemctl start  pqb-research.timer pqb-backup.timer >/dev/null 2>&1 || true

# ----------------------------------------------------------------- verdict
say "Verifying the installation"
if sudo -u "$APP_USER" env "PQB_POLYMARKETBOT_PATH=${UPSTREAM_DIR}" \
     "PQB_QUANT_BRIDGE_PATH=${BRIDGE_DIR}" \
     "$VENV/bin/python" -m pqb.cli --config "${APP_DIR}/config/config.yaml" check
then
  CHECK_OK=1
else
  CHECK_OK=0
fi

echo
if [ "$missing" -eq 1 ]; then
  warn "One or more sibling projects are missing (see above). Copy them, then"
  warn "re-run this script — it is safe to run again."
fi
if [ "$CHECK_OK" -ne 1 ]; then
  warn "'pqb.cli check' reported problems. Fix them before starting."
fi

cat <<EOF

Installed. The service is NOT started and the config is in DRY-RUN.

  Next:
    sudo -u ${APP_USER} ${VENV}/bin/python -m pqb.cli --config ${APP_DIR}/config/config.yaml check
    sudo systemctl start pqb
    journalctl -u pqb -f

  Read ${APP_DIR}/docs/VPS-GUIDE.md before enabling live trading.
EOF
