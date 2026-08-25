"""Position sizing, and the Win Expansion engine.

Two separate questions, kept separate because conflating them is how accounts
die:

  SIZING       given the account, how much is one unit of risk?
  EXPANSION    is THIS setup strong enough to deserve more than one unit?

Sizing is about the account and is bounded by GLOBAL_SAFETY. Expansion is about
the evidence and is bounded by the evidence. Neither may override the other:
the final stake is the MINIMUM of what expansion asks for and what safety
allows, never a compromise between them.

On Win Expansion specifically, the brief's constraint is the important part:

    Never increase size merely because a trade "looks good."

So `expand()` cannot return more than 1.00x unless every precondition is met
with a measured number attached, and every refusal names which precondition
failed. The ladder of multipliers is discovered by `fit_expansion`, not
assumed -- 1.5x is not privileged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import Settings


@dataclass
class SizingDecision:
    stake: float
    fraction: float
    base_stake: float
    multiplier: float
    mode: str
    reasons: list = field(default_factory=list)
    caps_applied: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"stake": round(self.stake, 2),
                "fraction": round(self.fraction, 5),
                "base_stake": round(self.base_stake, 2),
                "multiplier": round(self.multiplier, 3), "mode": self.mode,
                "reasons": self.reasons, "caps_applied": self.caps_applied}


def kelly_fraction(win_prob: float, price: float) -> float:
    """Kelly for a binary payoff bought at `price`.

    Win pays (1-price)/price per unit staked; a loss costs the stake. This is
    the FULL Kelly and is never used directly -- `size()` applies
    `sizing.kelly_fraction` on top, and the brief's rule stands: never deploy
    theoretical Kelly blindly. Full Kelly on a mis-estimated probability is a
    reliable way to reach zero.
    """
    if not (0 < price < 1):
        return 0.0
    b = (1.0 - price) / price
    q = 1.0 - win_prob
    f = (b * win_prob - q) / b
    return max(0.0, min(f, 1.0))


def base_size(st: Settings, equity: float, *, win_prob: float = 0.0,
              price: float = 0.0, expectancy: float = 0.0,
              confidence: float = 0.0, drawdown: float = 0.0) -> SizingDecision:
    """One unit of risk, before any expansion."""
    cfg = st.sizing
    reasons: list = []
    mode = cfg.mode

    if mode == "fixed":
        fraction = cfg.base_fraction
        reasons.append("fixed stake")
    elif mode == "edge":
        # Scale with measured expectancy, saturating: twice the edge does not
        # justify twice the risk once the estimate's own error dominates.
        scale = min(2.0, max(0.0, expectancy) / 0.05) if expectancy > 0 else 0.0
        fraction = cfg.base_fraction * scale
        reasons.append(f"edge-scaled on expectancy {expectancy:+.4f}")
    elif mode == "confidence":
        fraction = cfg.base_fraction * min(2.0, max(0.0, confidence) / 0.5)
        reasons.append(f"confidence-scaled at {confidence:.2f}")
    elif mode == "kelly":
        f = kelly_fraction(win_prob, price) * cfg.kelly_fraction
        fraction = min(f, cfg.base_fraction * 3)
        reasons.append(f"{cfg.kelly_fraction:.2f} fractional Kelly "
                       f"(full would be {kelly_fraction(win_prob, price):.3f})")
    else:
        fraction = cfg.base_fraction
        reasons.append("fixed fractional of equity")

    caps = []
    # Drawdown de-risking. Not optional and not a strategy choice: shrinking
    # risk as equity falls is what turns a drawdown into a recoverable one.
    if drawdown > cfg.drawdown_derisk_at:
        shrink = max(0.25, 1.0 - (drawdown - cfg.drawdown_derisk_at) * 3.0)
        fraction *= shrink
        caps.append(f"drawdown {drawdown:.1%} -> size x{shrink:.2f}")

    fraction = max(0.0, min(fraction, st.risk.max_fraction_per_trade))
    stake = max(0.0, equity * fraction)
    return SizingDecision(stake=stake, fraction=fraction, base_stake=stake,
                          multiplier=1.0, mode=mode, reasons=reasons,
                          caps_applied=caps)


@dataclass
class ExpansionEvidence:
    """What must be true before a stake may exceed 1.00x. Every field is a
    measurement, not an opinion."""

    sample_size: int = 0
    expectancy: float = 0.0
    oos_expectancy: float = 0.0
    max_drawdown_pct: float = 0.0
    risk_of_ruin: float = 0.0
    portfolio_concentration: float = 0.0
    correlation: float = 0.0
    available_depth: float = 0.0
    behavior_match: float = 0.0
    strategy_score: float = 0.0
    size_predicts_win: float = 0.0     # measured on the wallet, not assumed


def expand(st: Settings, ev: ExpansionEvidence,
           stake: float) -> tuple[float, list, list]:
    """Decide the multiplier. Returns (multiplier, reasons, blockers).

    Every precondition below can only ever REDUCE the multiplier. There is no
    path through this function where a strategy talks its way up.
    """
    cfg = st.sizing
    blockers: list = []
    reasons: list = []

    if ev.sample_size < cfg.expansion_min_sample:
        blockers.append(f"sample {ev.sample_size} < {cfg.expansion_min_sample}")
    if ev.oos_expectancy < cfg.expansion_min_expectancy:
        blockers.append(f"out-of-sample expectancy {ev.oos_expectancy:+.4f} < "
                        f"{cfg.expansion_min_expectancy:+.4f}")
    if ev.expectancy <= 0:
        blockers.append(f"in-sample expectancy {ev.expectancy:+.4f} not positive")
    if ev.max_drawdown_pct > cfg.expansion_max_drawdown:
        blockers.append(f"drawdown {ev.max_drawdown_pct:.1%} > "
                        f"{cfg.expansion_max_drawdown:.0%}")
    if ev.risk_of_ruin > 0.02:
        blockers.append(f"risk of ruin {ev.risk_of_ruin:.1%} > 2%")
    if ev.portfolio_concentration > st.risk.max_strategy_share:
        blockers.append(f"strategy already {ev.portfolio_concentration:.0%} of "
                        "exposure")
    if ev.correlation > st.risk.max_correlated_share:
        blockers.append(f"correlated exposure {ev.correlation:.0%} too high")
    if ev.available_depth and ev.available_depth < stake * 3:
        blockers.append(f"depth ${ev.available_depth:,.0f} cannot absorb "
                        f"{stake * cfg.max_expansion:,.0f} at full expansion")

    if blockers:
        return 1.00, ["expansion withheld"], blockers

    # All preconditions met. The multiplier is driven by the two things that
    # were actually measured to matter, and capped by the ladder.
    strength = 0.5 * min(1.0, ev.strategy_score / 0.6) \
        + 0.5 * min(1.0, max(0.0, ev.behavior_match - 0.5) / 0.4)

    # If the reference wallet's own size carries no information about winning,
    # sizing up on "conviction" is superstition. Measured in
    # decompose.SizingModel.size_predicts_win.
    if ev.size_predicts_win <= 0.0:
        strength *= 0.5
        reasons.append("wallet size does not predict wins; expansion damped")

    ladder = [m for m in cfg.expansion_ladder if m <= cfg.max_expansion]
    idx = min(len(ladder) - 1, int(strength * len(ladder)))
    mult = ladder[idx]
    reasons.append(f"strength {strength:.2f} -> {mult:.2f}x from ladder "
                   f"{ladder}")
    return mult, reasons, []


def size(st: Settings, equity: float, ev: ExpansionEvidence, *,
         win_prob: float = 0.0, price: float = 0.0,
         drawdown: float = 0.0) -> SizingDecision:
    """The full sizing decision: base unit, then expansion, then hard caps."""
    d = base_size(st, equity, win_prob=win_prob, price=price,
                  expectancy=ev.oos_expectancy, confidence=ev.strategy_score,
                  drawdown=drawdown)
    mult, reasons, blockers = expand(st, ev, d.base_stake)
    d.multiplier = mult
    d.reasons.extend(reasons)
    if blockers:
        d.reasons.append("blocked by: " + "; ".join(blockers))
    d.stake = d.base_stake * mult

    # GLOBAL_SAFETY has the last word. Expansion cannot lift a stake past the
    # per-trade cap -- that is what makes the cap a cap.
    hard = equity * st.risk.max_fraction_per_trade
    if d.stake > hard:
        d.caps_applied.append(
            f"per-trade cap: ${d.stake:,.0f} -> ${hard:,.0f} "
            f"({st.risk.max_fraction_per_trade:.1%} of equity)")
        d.stake = hard
    if st.costs.max_notional and d.stake > st.costs.max_notional:
        d.caps_applied.append(f"execution notional cap ${st.costs.max_notional:,.0f}")
        d.stake = st.costs.max_notional
    d.fraction = d.stake / equity if equity > 0 else 0.0
    return d


def fit_expansion(fills, ladder=None) -> dict:
    """DISCOVER the sizing function rather than assume it.

    Re-scores historical fills under each multiplier applied to the strongest
    setups, and reports what each would have done to expectancy, drawdown and
    the loss tail. The answer is often "1.00x": a multiplier that improves the
    mean while doubling the tail is not an improvement, and this table makes
    that visible instead of burying it in a single P&L number.
    """
    if not fills:
        return {"rows": [], "recommended": 1.0, "note": "no fills"}
    ladder = ladder or (1.00, 1.10, 1.25, 1.50, 2.00)

    # "Strong" is defined by an ENTRY-TIME feature (the wallet's own relative
    # conviction), never by the outcome. Using the outcome here would produce a
    # spectacular and entirely fictional result.
    strong = [f for f in fills if f.rel_notional >= 1.5]
    if len(strong) < 10:
        return {"rows": [], "recommended": 1.0,
                "note": f"only {len(strong)} high-conviction fills; cannot fit"}

    rows = []
    for m in ladder:
        pnl = 0.0
        stake = 0.0
        rets = []
        eq = 0.0
        peak = 0.0
        worst = 0.0
        for f in fills:
            k = m if f.rel_notional >= 1.5 else 1.0
            s = f.stake * k
            p = s * f.ret
            pnl += p
            stake += s
            rets.append(f.ret)
            eq += p
            peak = max(peak, eq)
            worst = min(worst, eq - peak)
        losses = sorted(r for r in rets if r < 0)
        k5 = max(1, len(losses) // 20)
        rows.append({
            "multiplier": m, "pnl": round(pnl, 2),
            "roi": round(pnl / stake, 5) if stake else 0.0,
            "max_drawdown": round(abs(worst), 2),
            "drawdown_per_pnl": round(abs(worst) / pnl, 3) if pnl > 0 else 0.0,
            "tail_loss_p05": round(sum(losses[:k5]) / k5, 4) if losses else 0.0,
            "n_expanded": len(strong),
        })

    # Recommend on return per unit of drawdown, not on return. The ladder step
    # that makes the most money is almost always the largest one; the step that
    # makes the most money per unit of pain usually is not.
    scored = [(r["pnl"] / (r["max_drawdown"] or 1.0), r) for r in rows
              if r["pnl"] > 0]
    if not scored:
        return {"rows": rows, "recommended": 1.0,
                "note": "no multiplier is profitable on this sample"}
    best = max(scored, key=lambda t: t[0])[1]
    return {"rows": rows, "recommended": best["multiplier"],
            "note": (f"{best['multiplier']:.2f}x maximises return per unit of "
                     f"drawdown on {len(fills)} fills "
                     f"({len(strong)} high-conviction). This is a FIT and must "
                     "be validated out-of-sample before use.")}
