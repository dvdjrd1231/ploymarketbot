"""§40 — the outbound half. A discovery that reaches nobody is not a monitor.

Until now the surfacer ranked what it noticed and waited for someone to open
the console. That is fine for "a gate has been forgoing more than it avoids"
and useless for "the account is one bad trade from the hard stop", and §40
prioritises by URGENCY precisely so the second kind can be treated differently.

Three channels, all off-by-default except the first, because the project's
standing rule is that nothing dials out unless a human configured it:

    console   the reply attachment. Always on, costs nothing, needs no
              configuration, and reaches the user the moment they next look.
    file      one JSON object per line in `var/notifications.jsonl`. Always
              on. Local, tailable, and the thing an external script can watch
              without this codebase growing an integration.
    webhook   HTTP POST to `PQV3_WEBHOOK_URL`. Off unless that variable is
              set. Fires only above `URGENT` — a webhook that relays every
              finding is a webhook someone mutes in a week, and a muted urgent
              channel is worse than no urgent channel because it looks like
              coverage.

The urgency floor is deliberately far above the console's. `PRIORITY_FLOOR`
(0.18) decides what is worth reading; `URGENT` (0.55) decides what is worth
interrupting a human who is not looking at the screen. Between them sits the
large majority of true findings, and leaving them in the console is the
correct treatment.

NO SECRET LEAVES. Every payload passes through `secrets.scrub`, and the
webhook body carries the finding and its measurement, never store rows, never
configuration, never a wallet address.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..secrets import scrub

URGENT = 0.55
WEBHOOK_ENV = "PQV3_WEBHOOK_URL"
TIMEOUT_SECS = 6.0


def webhook_url() -> str:
    return (os.environ.get(WEBHOOK_ENV) or "").strip()


def channels() -> dict:
    url = webhook_url()
    return {
        "console": {"enabled": True,
                    "detail": "attached to the next console reply"},
        "file": {"enabled": True, "detail": "var/notifications.jsonl"},
        "webhook": {
            "enabled": bool(url),
            "detail": (f"POST to a configured endpoint above priority {URGENT}"
                       if url else
                       f"not configured. Set {WEBHOOK_ENV} to enable. Nothing "
                       f"dials out until you do"),
            # The URL itself is never echoed: it frequently embeds a token.
            "configured": bool(url)},
        "urgent_floor": URGENT,
    }


def _record(work_dir: Path, payload: dict) -> bool:
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        with (work_dir / "notifications.jsonl").open(
                "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str) + "\n")
        return True
    except Exception:                                         # noqa: BLE001
        return False


def _post(url: str, payload: dict) -> tuple[bool, str]:
    body = json.dumps(payload, default=str).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "polymarket-quant-bridge-v3/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECS) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:                                    # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def send(st, discoveries: list) -> dict:
    """Deliver what clears the urgency floor. Returns what actually happened.

    Delivery failure is reported, never raised and never retried in a loop: a
    monitor that blocks the research loop on an unreachable endpoint has become
    the outage it was installed to report.
    """
    urgent = [d for d in discoveries
              if float(d.get("priority") or 0) >= URGENT]
    out = {"considered": len(discoveries), "urgent": len(urgent),
           "file": 0, "webhook": {"attempted": 0, "delivered": 0,
                                  "errors": []},
           "urgent_floor": URGENT}
    if not discoveries:
        out["note"] = "nothing to deliver"
        return out

    work = Path(getattr(st, "work_dir", "var"))
    for d in discoveries:
        payload = scrub({
            "ts": int(time.time()), "kind": d.get("kind"),
            "headline": d.get("headline"), "measured": d.get("measured"),
            "why": d.get("why"), "action": d.get("action"),
            "priority": d.get("priority"),
            "urgent": float(d.get("priority") or 0) >= URGENT,
            "mode": getattr(getattr(st, "mode", None), "value", ""),
        })
        if _record(work, payload):
            out["file"] += 1

    url = webhook_url()
    if url and urgent:
        for d in urgent:
            out["webhook"]["attempted"] += 1
            ok, detail = _post(url, scrub({
                "source": "polymarket-quant-bridge-v3",
                "mode": getattr(getattr(st, "mode", None), "value", ""),
                "priority": d.get("priority"), "kind": d.get("kind"),
                "headline": d.get("headline"), "measured": d.get("measured"),
                "why": d.get("why"), "action": d.get("action"),
                "note": "priority is an ESTIMATE used to rank; it is not a "
                        "measurement",
            }))
            if ok:
                out["webhook"]["delivered"] += 1
            else:
                out["webhook"]["errors"].append(detail)

    out["note"] = (
        f"{out['file']} written to notifications.jsonl. "
        + (f"{out['webhook']['delivered']}/{out['webhook']['attempted']} "
           f"delivered by webhook" if url else
           f"webhook not configured; {len(urgent)} urgent finding(s) are in "
           f"the file and the console only")
        + (". Delivery failures are reported, never retried in a loop — a "
           "monitor that blocks on an unreachable endpoint becomes the outage"
           if out["webhook"]["errors"] else ""))
    return out
