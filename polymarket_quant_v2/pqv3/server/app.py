"""The local HTTP server. Stdlib only, bound to loopback.

`ThreadingHTTPServer` rather than the single-threaded default: the dashboard
polls every 30 seconds and some sections issue a handful of queries, so a
single-threaded server would let one slow section block the whole page.

Bound to 127.0.0.1 by default and it stays that way unless a human passes
`--host`. There is no authentication here, and adding one would be security
theatre over a loopback socket — the correct control is not listening on a
routable interface, which is what the default does.
"""

from __future__ import annotations

import json
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..config import Settings
from ..secrets import wallet_banner
from .api import Api
from .ui import page


def make_handler(st: Settings, api: Api, engine):
    class Handler(BaseHTTPRequestHandler):
        server_version = "pqv3"

        # Silence per-request logging: a dashboard polling every 30s would
        # otherwise bury the console the operator is trying to read.
        def log_message(self, fmt, *args):
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            # The dashboard is entirely self-contained; nothing should be able
            # to pull a remote script into a page that renders account data.
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; connect-src 'self'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, default=str).encode(),
                       "application/json; charset=utf-8")

        def do_GET(self) -> None:                             # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = urllib.parse.parse_qs(parsed.query)

            if path == "/":
                html = page(mode=st.mode.value, wallet=wallet_banner(),
                            live_authorized=st.live_authorized,
                            starting_capital=st.capital.starting_capital,
                            url=st.server.url)
                return self._send(200, html.encode(), "text/html; charset=utf-8")

            if path == "/api":
                return self._json({"sections": list(Api.ROUTES)})

            if path.startswith("/api/"):
                name = path[5:]
                try:
                    if name == "wallet_detail":
                        return self._json(api.get(
                            "wallet_detail", wallet=(qs.get("wallet") or [""])[0]))
                    payload = api.get(name)
                    return self._json(payload,
                                      404 if "error" in payload else 200)
                except Exception as e:                        # noqa: BLE001
                    # A broken section must not take the dashboard down; it
                    # should render its own error so the operator can see which.
                    return self._json({"error": f"{type(e).__name__}: {e}",
                                       "section": name}, 500)

            if path == "/healthz":
                return self._json({"ok": True, "mode": st.mode.value,
                                   "engine": engine.status() if engine else None})

            self._json({"error": "not found", "path": path}, 404)

    return Handler


class Dashboard:
    def __init__(self, st: Settings, engine) -> None:
        self.st = st
        self.engine = engine
        self.api = Api(st, engine.store, engine)
        self.httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def serve(self, *, block: bool = True) -> str:
        handler = make_handler(self.st, self.api, self.engine)
        self.httpd = ThreadingHTTPServer(
            (self.st.server.host, self.st.server.port), handler)
        url = self.st.server.url
        if self.st.server.open_browser:
            threading.Timer(0.6, lambda: webbrowser.open(url)).start()
        if not block:
            self._thread = threading.Thread(target=self.httpd.serve_forever,
                                            daemon=True, name="pqv3-http")
            self._thread.start()
            return url
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return url

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
