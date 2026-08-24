"""
Editing the config from the dashboard, without destroying it.

The config file is heavily commented — every setting has an explanation of what
it does and why the default is what it is. Round-tripping it through a YAML
dumper would silently delete all of that, so this edits the specific lines in
place instead: find ``  key: value`` at the right nesting depth, replace the
value, leave everything else byte-for-byte alone.

Only the settings listed in :data:`EDITABLE` are exposed. The rest are either
structural (hosts, file paths) or dangerous to change from a GUI without
understanding them, and a settings screen that offers a hundred knobs to someone
who wants three is not a usable settings screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Setting:
    path: str                 # dotted path, e.g. "engine.portfolio.min_order_usdc"
    label: str                # what a person calls it
    help: str                 # one line, plain English
    kind: str                 # "float" | "int" | "percent" | "money" | "bool"
    low: float = 0.0
    high: float = 1.0
    group: str = "Trading"


# The one thing this screen must NOT do is let anyone hand-set a STRATEGY. What
# to buy, when to take profit, when to cut, when to reduce — those are the rules
# the Quant Bridge DISCOVERS for itself from the data (`pqb.cli research`). A
# slider for "take profit at X%" would be imposing the very rule the system
# exists to find, and would turn a self-discovering brain into a generic
# terminal. So the only things exposed here are ACCOUNT settings and SAFETY
# limits — the guardrails around the brain, never the brain's decisions.
NOTE = ("Strategy is DISCOVERED by the Quant Bridge, not set here — including "
        "how many positions to hold, when to stop, and how much cash to keep. "
        "These are only your account basics and operational settings. Leave a "
        "value at 0 where you want no limit at all and the brain to decide.")

EDITABLE: tuple[Setting, ...] = (
    # --- account -------------------------------------------------------------
    # Only genuine ACCOUNT basics. No position count, no drawdown pause, no cash
    # reserve — the decision engine owns all of those. Ranges are deliberately
    # wide so nothing a person types is clamped to a floor they did not choose.
    Setting("engine.portfolio.max_position_fraction", "Most per trade",
            "Largest share of the account any single trade may use.",
            "percent", 0.00001, 1.0, "Account"),
    Setting("engine.portfolio.min_order_usdc", "Smallest trade",
            "Never trade less than this. Fees hurt most on tiny trades.",
            "money", 0.01, 100.0, "Account"),
    Setting("engine.portfolio.fee_per_trade_usdc", "Fee per trade",
            "Charged on the way in AND out, so P&L is honest.",
            "money", 0.0, 1.0, "Account"),
    Setting("engine.portfolio.fee_fund_usdc", "Fee fund",
            "Set aside for fees only. Auto-refills from the wallet when it "
            "drops to 10% of this amount.",
            "money", 0.0, 100.0, "Account"),
    # Optional and OFF by default: an operator-set drawdown pause. 0 in the
    # file means off — which is why the range starts at 0 — and the dashboard
    # shows it as an ON/OFF switch beside the percentage. When off, the brain
    # manages risk with nothing imposed; when on, new buying pauses once the
    # account is this far below its peak. The bot itself needs no change:
    # the engine has always treated 0 as "no pause".
    Setting("engine.portfolio.max_drawdown_pct", "Stop buying if down",
            "Pause new trades once the account is this far below its peak. "
            "Switch OFF to let the brain manage this itself.",
            "percent", 0.0, 0.9, "Account"),
    # Same 0-means-off contract as the drawdown pause, but this one ships ON:
    # a cash buffer the engine never spends. The fee fund covers fees; this
    # covers everything else a fully-deployed account cannot pay for.
    Setting("engine.portfolio.reserve_cash_fraction", "Always keep in cash",
            "A share of the account it will never spend on positions. "
            "Switch OFF to let it deploy everything.",
            "percent", 0.0, 0.9, "Account"),
    # A pure ON/OFF: the per-category windows live in the config file with the
    # operator's matrix written out beside them.
    Setting("engine.exits.stagnation_enabled", "Close stagnant trades",
            "Prune a position showing no momentum within its window - minutes "
            "for fast markets, hours for event markets - to free the capital.",
            "bool", 0.0, 1.0, "Account"),

    # --- capital preservation ------------------------------------------------
    # These belong on this screen for the same reason the cash reserve does:
    # they are SAFETY LIMITS, not strategy. None of them can say what to buy,
    # when to take profit or when to cut — each one only shrinks or pauses what
    # the brain already decided, and none of them can block an EXIT. They are
    # fractions of the account rather than dollar amounts so that they keep
    # meaning the same thing as the balance moves.
    Setting("risk.preservation.enabled", "Protect the account",
            "Bounded caps that keep a bad run from ending the account. They "
            "only ever shrink or pause new buying — never sell, never size up.",
            "bool", 0.0, 1.0, "Protection"),
    Setting("risk.preservation.min_cash_reserve_fraction",
            "Stop buying below this much cash",
            "New trades pause when cash falls to this share of the account. "
            "Exits still run and refill it.",
            "percent", 0.0, 0.9, "Protection"),
    Setting("risk.preservation.max_total_exposure_fraction",
            "Most of the account in trades at once",
            "The total value of everything open, as a share of the account.",
            "percent", 0.0, 1.0, "Protection"),
    Setting("risk.preservation.max_cluster_fraction",
            "Most in one bet",
            "Positions in the same market, or on the same trader's thesis, "
            "are ONE bet however many of them there are. This caps that.",
            "percent", 0.0, 1.0, "Protection"),
    Setting("risk.preservation.halt_entries_drawdown_pct",
            "Pause new trades if down",
            "New buying stops entirely once the account is this far below its "
            "peak. Open positions are still managed and still exit.",
            "percent", 0.0, 0.95, "Protection"),
    Setting("risk.preservation.shrink_from_drawdown_pct",
            "Start trading smaller if down",
            "Below this much drawdown, stakes begin shrinking.",
            "percent", 0.0, 0.9, "Protection"),
    Setting("risk.preservation.shrink_to_drawdown_pct",
            "...fully small by",
            "The drawdown at which stakes reach their smallest allowed size.",
            "percent", 0.0, 0.95, "Protection"),
    Setting("risk.preservation.min_size_scale",
            "Smallest it will ever trade",
            "Stakes never shrink below this share of normal — the validated "
            "strategy must stay able to express its edge.",
            "percent", 0.05, 1.0, "Protection"),

    # --- operational ---------------------------------------------------------
    Setting("markets.filters.max_markets", "Markets to watch",
            "More markets means more chances but more work each cycle.",
            "int", 5, 1000, "Operational"),
    Setting("markets.filters.min_liquidity", "Skip markets thinner than",
            "Thin markets are hard to get out of.",
            "money", 0.0, 200_000.0, "Operational"),
    Setting("engine.cycle_seconds", "Check every (seconds)",
            "How often it looks at the market and decides.",
            "int", 1, 300, "Operational"),
)

_GROUPS = ("Account", "Operational")


def groups() -> tuple[str, ...]:
    return _GROUPS


def read(config_path: Path) -> dict[str, Any]:
    """Current values, straight from the file rather than the parsed config.

    Read from the file so what the screen shows is what the file says — if the
    two ever disagree, the file is what the bot will load on its next start.
    """
    text = config_path.read_text(encoding="utf-8")
    out: dict[str, Any] = {}
    for setting in EDITABLE:
        found = _find(text, setting.path)
        if found is not None:
            out[setting.path] = _coerce(found[2], setting.kind)
    return out


def write(config_path: Path, values: dict[str, Any]) -> list[str]:
    """Apply changed values in place. Returns a description of what changed.

    Writes via a temporary file and a replace, so an interrupted save cannot
    leave the operator with a half-written config and a bot that will not start.
    """
    text = config_path.read_text(encoding="utf-8")
    changed: list[str] = []
    for setting in EDITABLE:
        if setting.path not in values:
            continue
        found = _find(text, setting.path)
        if found is None:
            continue
        start, end, current = found
        # Compared as NUMBERS, not as text. "1.0" and "1" are the same setting,
        # and treating them as a change means every save rewrites the file and
        # reports edits nobody made.
        if _coerce(current, setting.kind) == _coerce(
                _format(values[setting.path], setting.kind), setting.kind):
            continue
        new = _format(values[setting.path], setting.kind)
        # Keep the column width so the aligned end-of-line comments in this
        # file stay aligned after an edit.
        if len(new) < len(current):
            new = new.ljust(len(current))
        changed.append(f"{setting.label}: {current.strip()} -> {new.strip()}")
        text = text[:start] + new + text[end:]

    if changed:
        tmp = config_path.with_suffix(config_path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(config_path)
    return changed


def _find(text: str, dotted: str):
    """Locate ``key:``'s value under the right parents. Returns (start, end, raw).

    Matched by walking the parent keys in order and requiring increasing
    indentation, so ``engine.exits.stop_loss_pct`` cannot accidentally match a
    ``stop_loss_pct`` under a different section.
    """
    parts = dotted.split(".")
    pos, indent = 0, -1
    for depth, part in enumerate(parts):
        last = depth == len(parts) - 1
        # `value` stops before any trailing spaces, so the gap in front of an
        # end-of-line comment survives a rewrite. Without that separation the
        # replacement butts straight onto the `#`, YAML stops treating it as a
        # comment, and the whole thing parses as a STRING — a config that still
        # loads but hands the engine "0.72# 0-1 blended…" where it expects a
        # number.
        pattern = re.compile(
            rf"^(?P<indent>[ ]*){re.escape(part)}:[ ]*"
            rf"(?P<value>[^\n#]*?)(?=[ ]*(?:#|$))",
            re.MULTILINE)
        match = None
        for candidate in pattern.finditer(text, pos):
            if len(candidate.group("indent")) > indent:
                match = candidate
                break
        if match is None:
            return None
        indent = len(match.group("indent"))
        pos = match.end() if not last else pos
        if last:
            value = match.group("value")
            return (match.start("value"), match.start("value") + len(value),
                    value)
    return None


def _coerce(raw: str, kind: str):
    raw = raw.strip().strip('"').strip("'")
    try:
        if kind == "int":
            return int(float(raw))
        if kind == "bool":
            return raw.lower() in ("true", "yes", "1")
        return float(raw)
    except ValueError:
        return 0


def _format(value: Any, kind: str) -> str:
    if kind == "bool":
        return "true" if value else "false"
    if kind == "int":
        return str(int(value))
    # 8 decimals, not 4: a "most per trade" of 0.001% is a fraction of 0.00001,
    # which 4 places rounded to 0 — the value silently became "0" on save.
    text = f"{float(value):.8f}".rstrip("0").rstrip(".")
    return text or "0"
