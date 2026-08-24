#!/usr/bin/env bash
#
# Build the self-contained image.
#
#   bash deploy/docker/build.sh
#
# Docker accepts exactly one build context, but this system spans three
# repositories. Rather than build from a shared parent — which would drag every
# unrelated sibling project into the context — this stages just the three into
# a temporary directory and builds from that.
#
# The result is a single image with the layout already correct inside it, which
# removes the most common way a manual install fails.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(cd "${HERE}/../.." && pwd)"
PARENT="$(dirname "$PROJECT")"

IMAGE="${IMAGE:-pqb:latest}"
UPSTREAM_SRC="${UPSTREAM_SRC:-${PARENT}/ploymarketbot}"
# The INNER qc_lean_bridge folder, not the outer "Advanced Strategy
# Development for QuantConnect LEAN" wrapper.
BRIDGE_SRC="${BRIDGE_SRC:-}"

say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

if [ -z "$BRIDGE_SRC" ]; then
  for candidate in \
    "${PARENT}/Advanced Strategy Development for QuantConnect LEAN/qc_lean_bridge" \
    "${PARENT}/qc_lean_bridge"; do
    [ -f "${candidate}/core/pipeline.py" ] && { BRIDGE_SRC="$candidate"; break; }
  done
fi

[ -f "${UPSTREAM_SRC}/backend/services/trading.py" ] || die \
  "ploymarketbot not found at '${UPSTREAM_SRC}'. Set UPSTREAM_SRC=/path/to/it."
[ -n "$BRIDGE_SRC" ] && [ -f "${BRIDGE_SRC}/core/pipeline.py" ] || die \
  "qc_lean_bridge not found. Set BRIDGE_SRC=/path/to/qc_lean_bridge (the INNER folder)."

say "Staging the three repositories"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Excludes matter for more than size. Copying a host .venv into the image would
# shadow the one built inside it with binaries compiled for the wrong platform;
# copying a source tree's runtime output would bake somebody's local test run
# into a supposedly clean image.
COMMON_EXCLUDES=(.venv __pycache__ '*.pyc' .git .pytest_cache node_modules
                 dist build state .env .stagetest)

copy() {
  local src="$1" dst="$2"; shift 2
  local extra=("$@") args=() e
  mkdir -p "$dst"
  for e in "${COMMON_EXCLUDES[@]}" "${extra[@]}"; do args+=("--exclude=$e"); done
  if command -v rsync >/dev/null 2>&1; then
    rsync -a "${args[@]}" "${src}/" "${dst}/"
  else
    tar -C "$src" "${args[@]}" -cf - . | tar -C "$dst" -xf -
  fi
}

copy "$PROJECT"      "${STAGE}/polymarket-quant-bridge"
copy "$UPSTREAM_SRC" "${STAGE}/ploymarketbot"
# The Quant Bridge writes its own outputs beside its source: `data/` holds its
# SQLite registry (100 MB+ after a few local runs), and `strategies/`,
# `reports/` and `logs/` are the products of whoever last ran the desktop app.
# None of it is source, none of it is read on the paths this project uses — the
# research run points the bridge at our own export and output directories — and
# shipping it would carry one machine's test run into every deployment.
copy "$BRIDGE_SRC"   "${STAGE}/qc_lean_bridge" data logs reports strategies

echo "  polymarket-quant-bridge  <- ${PROJECT}"
echo "  ploymarketbot            <- ${UPSTREAM_SRC}"
echo "  qc_lean_bridge           <- ${BRIDGE_SRC}"

cp "${HERE}/Dockerfile" "${STAGE}/Dockerfile"
cat > "${STAGE}/.dockerignore" <<'IGNORE'
**/.venv
**/__pycache__
**/*.pyc
**/.git
**/.pytest_cache
**/node_modules
**/state
**/.env
IGNORE

say "Building ${IMAGE}"
docker build -t "$IMAGE" "$STAGE" "$@"

say "Verifying the image can see all three projects"
docker run --rm --entrypoint python "$IMAGE" -c "
import pqb.upstream, pqb.quant
ok, why = pqb.quant.available()
print('  ploymarketbot :', pqb.upstream.upstream_root())
print('  quant bridge  :', pqb.quant.bridge_root() if ok else 'FAILED: ' + why)
raise SystemExit(0 if ok else 1)
"

cat <<EOF

Built ${IMAGE}.

  cp config/config.example.yaml config/config.yaml    # if you have not already
  cp deploy/docker/pqb.env.example .env               # leave the key blank
  docker compose up -d
  docker compose logs -f

Read docs/VPS-GUIDE.md before enabling live trading.
EOF
