#!/usr/bin/env bash
#
# Container entrypoint. Everything after the image name is passed straight to
# the CLI, so the container is the CLI:
#
#   docker compose up -d                        -> pqb.cli run
#   docker compose run --rm pqb check           -> pqb.cli check
#   docker compose run --rm pqb wallets         -> pqb.cli wallets
#   docker compose run --rm pqb research        -> pqb.cli research
#
# Its only other job is making a container started against an EMPTY state
# volume come up correctly: seed the config, and fail loudly rather than
# silently mis-trade if something essential is missing.
set -euo pipefail

# /app inside the image. Overridable purely so these guards can be exercised
# outside a container — they are the last thing between a misconfigured
# deployment and real money, and untested guards are not guards.
APP_DIR="${APP_DIR:-/app}"
CONFIG="${APP_DIR}/config/config.yaml"
EXAMPLE="${APP_DIR}/config/config.example.yaml"

# --- config ---------------------------------------------------------------
if [ ! -f "$CONFIG" ]; then
  if [ -f "$EXAMPLE" ]; then
    cp "$EXAMPLE" "$CONFIG"
    echo "[entrypoint] seeded config/config.yaml from the example (DRY-RUN)."
    echo "[entrypoint] Bind-mount your own config to override it."
  else
    echo "[entrypoint] FATAL: no config and no example to seed from." >&2
    exit 1
  fi
fi

# --- state ----------------------------------------------------------------
# A named volume is created root-owned by default; if this container cannot
# write its journal it would otherwise fail deep inside the first cycle with a
# confusing SQLite error rather than here with a fixable one.
if [ ! -w "${APP_DIR}/state" ]; then
  echo "[entrypoint] FATAL: ${APP_DIR}/state is not writable by uid $(id -u)." >&2
  echo "[entrypoint] Fix the volume's ownership on the host, e.g.:" >&2
  echo "[entrypoint]   sudo chown -R 10001:10001 ./state" >&2
  exit 1
fi

# --- the siblings ---------------------------------------------------------
# Baked into the image, so absence means a broken build rather than a missing
# copy step — worth saying explicitly instead of surfacing as an ImportError.
for path in "${PQB_POLYMARKETBOT_PATH:-/opt/ploymarketbot}/backend/services/trading.py" \
            "${PQB_QUANT_BRIDGE_PATH:-/opt/qc_lean_bridge}/core/pipeline.py"; do
  if [ ! -f "$path" ]; then
    echo "[entrypoint] FATAL: missing ${path} — the image was built without" >&2
    echo "[entrypoint] one of its sibling projects. Rebuild with build.sh." >&2
    exit 1
  fi
done

# --- live-mode guard ------------------------------------------------------
# Two flags plus a key are required to trade. If the config is armed but no key
# reached the container, the run would start, place nothing, and log rejections
# — which reads like a market problem rather than a missing env file.
# POSIX classes rather than \s: the latter is a GNU grep extension, and this
# guard is the last thing between a misconfigured container and real money.
if grep -qE '^[[:space:]]*dry_run:[[:space:]]*false' "$CONFIG" 2>/dev/null \
   && grep -qE '^[[:space:]]*allow_live:[[:space:]]*true' "$CONFIG" 2>/dev/null; then
  if [ -z "${PQB_PRIVATE_KEY:-}" ]; then
    echo "[entrypoint] FATAL: config is armed for LIVE trading but" >&2
    echo "[entrypoint] PQB_PRIVATE_KEY is empty. Pass it via --env-file." >&2
    exit 1
  fi
  echo "[entrypoint] *** LIVE MODE *** — real orders will be placed."
fi

exec python -m pqb.cli --config "$CONFIG" "$@"
