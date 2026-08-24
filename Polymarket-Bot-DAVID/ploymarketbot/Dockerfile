# syntax=docker/dockerfile:1
#
# Polymarket Copy Trading Bot.
#
# Three stages: the dashboard is built with Node, the Python dependencies are
# compiled in a throwaway image, and the runtime carries neither toolchain.
# Nothing needs Node or a specific Python on the host.
#
#   docker compose build && docker compose up -d
#
# See DEPLOY.md ("Running with Docker") for the full procedure.

# --- stage 1: build the dashboard -------------------------------------------
FROM node:20-slim AS dashboard

WORKDIR /build/frontend

# Copy the manifests alone first so `npm ci` is cached until they change.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build


# --- stage 2: python dependencies -------------------------------------------
FROM python:3.12-slim AS deps

# py-clob-client pulls in packages that may need a compiler when no wheel
# matches. None of this reaches the runtime image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev libssl-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip wheel \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt


# --- stage 3: runtime --------------------------------------------------------
FROM python:3.12-slim

# HOME drives app_data_dir() in backend/paths.py, so the database, config,
# bot.log and secret.enc all land in /data — which is a volume. Without this
# they would sit in the container's writable layer and vanish on rebuild.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME=/data

RUN useradd --uid 1000 --create-home --home-dir /home/polybot polybot \
    && mkdir -p /data \
    && chown polybot:polybot /data

COPY --from=deps /opt/venv /opt/venv

WORKDIR /app
COPY --chown=polybot:polybot backend/ ./backend/
COPY --chown=polybot:polybot run.py ./
COPY --from=dashboard --chown=polybot:polybot /build/frontend/dist ./frontend/dist

USER polybot
VOLUME ["/data"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=3).status == 200 else 1)"

# run.py is skipped deliberately: its frontend build and free-port search are
# meaningless here. These flags mirror the uvicorn settings it would apply.
CMD ["uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", "--port", "8765", \
     "--log-level", "warning", "--no-access-log", \
     "--ws-ping-interval", "20", "--ws-ping-timeout", "30"]
