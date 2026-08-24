"""
The code-enforced live-execution gate (§3, acceptance 11.7).

The spec is unambiguous: live trading stays blocked until the paper-mode record
shows a **calibrated, positive-net-EV, controlled-drawdown** edge over a
sufficient sample — and that this is enforced in code, not left as a guideline.
This module is that enforcement.

It reads the same journal the evidence report reads and answers one boolean
question: *may this session trade live?* Every failing condition is returned
with its number so the operator sees exactly why it is still blocked, and the
runner downgrades the session to paper rather than trading on an unproven model.

The conditions, all of which must hold:

* **Sample** — enough settled predictions to mean anything.
* **Beats the market** — our Brier score is lower than the market price's, which
  is the evidence the edge is real rather than a profitable week of luck.
* **Positive net P&L** — the realised, after-cost money went up, not just the
  paper equity at some peak.
* **Controlled drawdown** — the worst peak-to-trough on the realised sequence is
  shallower than the limit. A good average that hides a deep hole does not pass.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class GateResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)   # why it is blocked
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "reasons": self.reasons,
                "metrics": self.metrics}


def _rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        return []


def evaluate(journal_path: Path, cfg) -> GateResult:
    """Decide whether the paper record justifies arming live.

    ``cfg`` is a :class:`~pqb.config.LiveGateConfig`. A missing or empty journal
    is a clean fail, never an accidental pass — the safe default is "not proven".
    """
    if not Path(journal_path).exists():
        return GateResult(False, ["no journal yet — nothing has been proven"])

    conn = sqlite3.connect(str(journal_path))
    try:
        settled = _rows(
            conn, "SELECT probability, market_price, settled FROM predictions "
                  "WHERE settled IS NOT NULL")
        closed = _rows(
            conn, "SELECT realized_pnl FROM lifecycles WHERE status='CLOSED'")
    finally:
        conn.close()

    reasons: list[str] = []
    metrics: dict = {"settled": len(settled), "closedTrades": len(closed)}

    # 1. Sample size.
    if len(settled) < cfg.min_settled:
        reasons.append(
            f"only {len(settled)} settled predictions; need {cfg.min_settled}")

    # 2. Calibration vs the market (Brier). Only meaningful with a sample.
    if settled:
        ours = sum((r["probability"] - r["settled"]) ** 2
                   for r in settled) / len(settled)
        theirs = sum((r["market_price"] - r["settled"]) ** 2
                     for r in settled) / len(settled)
        metrics["brierOurs"] = round(ours, 4)
        metrics["brierMarket"] = round(theirs, 4)
        if cfg.require_beat_market and not (ours < theirs):
            reasons.append(
                f"our Brier {ours:.4f} does not beat the market's {theirs:.4f} "
                "— no evidence the edge is real")

    # 3 & 4. Realised P&L and drawdown, from closed trades.
    total = sum(r["realized_pnl"] for r in closed)
    equity = peak = worst = 0.0
    for r in closed:
        equity += r["realized_pnl"]
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    metrics["totalPnl"] = round(total, 2)
    metrics["worstDrawdown"] = round(worst, 2)

    if not closed:
        reasons.append("no closed trades yet to measure P&L or drawdown")
    else:
        if total < cfg.min_total_pnl_usdc:
            reasons.append(
                f"realised P&L ${total:,.2f} is below the required "
                f"${cfg.min_total_pnl_usdc:,.2f}")
        if worst < -abs(cfg.max_drawdown_usdc):
            reasons.append(
                f"worst drawdown ${worst:,.2f} is deeper than the allowed "
                f"${-abs(cfg.max_drawdown_usdc):,.2f}")

    return GateResult(passed=not reasons, reasons=reasons, metrics=metrics)
