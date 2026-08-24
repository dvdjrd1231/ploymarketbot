# Polymarket Quant Bridge — self-contained runtime image.
#
# Build it with deploy/docker/build.sh, NOT with `docker build .` from the
# project root. The image needs three separate repositories and Docker only
# accepts one build context, so build.sh stages all three into a temporary
# context first. That staging is the whole reason this image exists: getting
# the three-repo layout wrong is the single most common way a manual install
# fails, and inside an image it cannot be got wrong.
#
#   deploy/docker/build.sh          # build
#   docker compose up -d            # run
#
# The image deliberately contains no secrets and no config. Both arrive at
# runtime — the config as a bind mount, the key as an env file — so the same
# image is safe to move between machines and to keep in a registry.

FROM python:3.12-slim-bookworm

# sqlite3 for the healthcheck and backup scripts; tini so signals reach the
# application. The runner installs a SIGINT handler for graceful shutdown —
# without an init, PID 1 signal semantics would swallow it and every stop would
# be a hard kill mid-cycle.
RUN apt-get update \
 && apt-get install -y --no-install-recommends sqlite3 tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged, with a fixed UID so a bind-mounted state directory has
# predictable ownership on the host.
ARG PQB_UID=10001
RUN useradd --system --uid ${PQB_UID} --create-home --home-dir /home/pqb pqb

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=UTC \
    # The path shims resolve these rather than guessing from the layout.
    PQB_POLYMARKETBOT_PATH=/opt/ploymarketbot \
    PQB_QUANT_BRIDGE_PATH=/opt/qc_lean_bridge

WORKDIR /app

# Requirements first, so a code change does not re-resolve the dependency tree.
COPY polymarket-quant-bridge/requirements.txt /tmp/req-pqb.txt
COPY ploymarketbot/requirements.txt           /tmp/req-upstream.txt

# ONE environment for all three projects: the trading loop needs the Polymarket
# clients and the Quant Bridge's feature engineering inside a single process.
#
# The Quant Bridge's own requirements.txt is NOT installed — it pins PyQt6 for
# its desktop GUI (~85 MB plus X libraries) and nothing on the paths used here
# imports it. pandas and numpy, which are what it actually needs, come from
# this project's requirements.
RUN python -m pip install --upgrade pip wheel \
 && python -m pip install -r /tmp/req-pqb.txt \
 && python -m pip install -r /tmp/req-upstream.txt \
 && rm -f /tmp/req-pqb.txt /tmp/req-upstream.txt

# The two siblings, read-only at runtime — nothing writes to them.
COPY ploymarketbot/  /opt/ploymarketbot/
COPY qc_lean_bridge/ /opt/qc_lean_bridge/

# This project last: it changes most often.
COPY polymarket-quant-bridge/ /app/

# config/ and state/ are mount points. Seeded by the entrypoint on first run so
# a container started with an empty volume still comes up in dry-run.
RUN mkdir -p /app/state /app/config \
 && chown -R pqb:pqb /app /home/pqb \
 && chmod +x /app/deploy/docker/entrypoint.sh /app/scripts/*.sh

USER pqb
VOLUME ["/app/state"]

# Evidence-based: is the loop turning, is a halt engaged, is ingestion
# producing wallets. `--quiet` exits 0 healthy / 1 degraded / 2 unhealthy, and
# only 2 is reported as unhealthy — a degraded bridge (say, no strategies yet)
# is still doing useful work and must not be restarted out from under itself.
HEALTHCHECK --interval=60s --timeout=20s --start-period=180s --retries=3 \
  CMD bash /app/scripts/healthcheck.sh --quiet; [ $? -lt 2 ]

ENTRYPOINT ["/usr/bin/tini", "--", "/app/deploy/docker/entrypoint.sh"]
CMD ["run"]
