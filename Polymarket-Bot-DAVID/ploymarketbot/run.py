"""
Launcher for the Polymarket Copy Trading Bot web application.

    python run.py

Builds the dashboard if it has not been built yet, starts the local server, and
opens your browser. The server binds to 127.0.0.1 — it is reachable only from
this computer, never from your network or the internet.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FRONTEND = ROOT / "frontend"
DIST = FRONTEND / "dist"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _run(command: list[str], cwd: Path) -> int:
    """Run a command, streaming its output. Handles .cmd shims on Windows."""
    exe = shutil.which(command[0])
    if exe is None:
        print(f"  ! '{command[0]}' was not found on PATH.")
        return 127
    print(f"  > {' '.join(command)}")
    return subprocess.call([exe, *command[1:]], cwd=str(cwd))


def build_frontend(force: bool = False) -> bool:
    """Install dependencies and build the dashboard if needed."""
    index = DIST / "index.html"
    if index.exists() and not force:
        return True

    if not FRONTEND.exists():
        print("! frontend/ is missing — cannot build the dashboard.")
        return False

    if shutil.which("npm") is None:
        print(
            "! npm was not found on PATH.\n"
            "  Install Node.js 18+ from https://nodejs.org, then run this again.\n"
            "  (Node is only needed to build the dashboard, not to run the bot.)"
        )
        return False

    if not (FRONTEND / "node_modules").exists():
        print("Installing dashboard dependencies (first run only)…")
        if _run(["npm", "install"], FRONTEND) != 0:
            print("! npm install failed.")
            return False

    print("Building the dashboard…")
    if _run(["npm", "run", "build"], FRONTEND) != 0:
        print("! Dashboard build failed.")
        return False

    return index.exists()


def _free_port(host: str, port: int) -> int:
    """Return ``port`` if it is free, otherwise the next available one."""
    for candidate in range(port, port + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    raise SystemExit(f"No free port found in {port}–{port + 19}.")


def _open_browser(url: str) -> None:
    def target() -> None:
        # Give uvicorn a moment to bind before the tab requests the page.
        time.sleep(1.4)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=target, daemon=True).start()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the Polymarket copy trading bot web app.")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="Bind address (default 127.0.0.1 — local only).")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"Port (default {DEFAULT_PORT}).")
    parser.add_argument("--no-browser", action="store_true",
                        help="Do not open a browser window.")
    parser.add_argument("--rebuild", action="store_true",
                        help="Force a fresh dashboard build.")
    parser.add_argument("--reload", action="store_true",
                        help="Auto-reload the server on code changes (dev).")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "!! WARNING: binding to a non-local address exposes an API that can\n"
            "!! move funds. Only do this on a network you fully trust.\n"
        )

    try:
        import uvicorn
    except ImportError:
        print("! Dependencies are missing. Run:\n"
              "    pip install -r requirements.txt")
        return 1

    if not build_frontend(force=args.rebuild):
        print("\n! Continuing without a built dashboard — the API will run, but\n"
              "  http://localhost will show a build message instead of the UI.\n")

    port = _free_port(args.host, args.port)
    url = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{port}"

    print()
    print("  Polymarket Copy Trading Bot")
    print(f"  Dashboard : {url}")
    print(f"  API docs  : {url}/api/docs")
    print("  Press Ctrl+C to stop.")
    print()

    if not args.no_browser:
        _open_browser(url)

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=port,
        reload=args.reload,
        log_level="warning",
        access_log=False,
        ws_ping_interval=20,
        ws_ping_timeout=30,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
