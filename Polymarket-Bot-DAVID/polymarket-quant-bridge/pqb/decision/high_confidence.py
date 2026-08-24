"""
The high-confidence trade filter: the last gate before capital moves.

The operator's standalone module, implemented as briefed. The engine's own
scoring finds a candidate; THIS decides whether the evidence is exceptional
enough to act. Its final question, verbatim from the spec:

    "Is this a statistically validated, high-confidence opportunity that is
    currently supported by market structure, has positive expected value after
    execution costs, has limited contradictory evidence, and improves the
    portfolio?"

Checks, in evaluation order (cheap structure first, empirical history last):

  1. STATE       — enter while a move is being born (developing / impulse /
                   early continuation), never because it already happened.
  2. EXHAUSTION  — an impulse already dying is not an entry.
  3. LIQUIDITY   — visible depth must dwarf the stake; spread must be sane.
  4. EV          — probability vs the executable price, minus spread, slippage
                   and fees, must clear the configured minimum.
  5. CONTRADICTION — count the structure arguing AGAINST the trade (reversal,
                   draining book, imbalance against, abnormal spread, wallet
                   flow against). Too much and the trade is rejected however
                   good the rest looks.
  6. PORTFOLIO   — the position must improve the book, not just look good
                   alone: category concentration is capped.
  7. EMPIRICAL   — comparable historical setups (same category, same market
                   state, same score band) must exist in sufficient number,
                   with a strong win share — and the NEWEST slice of that
                   history, held out chronologically, must not contradict the
                   older slice the confidence came from. Optimising and
                   grading on the same data is the one sin this module exists
                   to prevent.

A setup with no history yet is "provisional": logged and allowed in paper
(else no history could ever accumulate), strictly rejected in live.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Score bands for "comparable setups": coarse on purpose — a 0.62 and a 0.64
# entry are the same kind of trade; slicing finer just starves every band.
_SCORE_BANDS = ((0.0, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 1.01))

_SETUP_CACHE_SECONDS = 300.0


@dataclass
class Verdict:
    allow: bool
    provisional: bool = False           # empirical gate had nothing to say
    reasons: list = field(default_factory=list)     # why it was rejected
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"allow": self.allow, "provisional": self.provisional,
                "reasons": self.reasons, **self.evidence}


def _band(score: float) -> str:
    for low, high in _SCORE_BANDS:
        if low <= score < high:
            return f"{low:.1f}-{high:.1f}"
    return "?"


class HighConfidenceFilter:
    """Stateful only for the setup-history cache; every check is pure."""

    def __init__(self, cfg, journal: Any = None, live: bool = False):
        self.cfg = cfg
        self.journal = journal
        self.live = live
        self._setups: dict[tuple, dict] = {}
        self._setups_at = 0.0

    # -- the gate ------------------------------------------------------------

    def evaluate(self, *, score: float, category: str, stake: float,
                 features: dict, spread: float, depth: float,
                 ev_per_dollar: Optional[float],
                 wallet_net: float, context: Any,
                 token_id: str = "") -> Verdict:
        cfg = self.cfg
        reasons: list[str] = []
        evidence: dict = {"scoreBand": _band(score)}

        state = float(features.get("ms_state", 0.0))
        if cfg.require_state and state not in tuple(cfg.allowed_states):
            reasons.append(
                f"market state {state:.0f} is not an entry state - a move "
                "that already happened is not a reason to join it")

        exhaustion = float(features.get("ms_exhaustion", 0.0))
        if exhaustion >= cfg.max_exhaustion:
            reasons.append(f"exhaustion {exhaustion:.0f} >= "
                           f"{cfg.max_exhaustion:.0f} - the impulse is dying")

        if spread > cfg.max_spread:
            reasons.append(f"spread {spread:.3f} beyond {cfg.max_spread:.3f}")
        if stake > 0 and depth > 0 and depth < cfg.min_depth_x_stake * stake:
            reasons.append(
                f"depth ${depth:,.0f} is under {cfg.min_depth_x_stake:.0f}x "
                f"the ${stake:,.0f} stake - the fill would eat the edge")

        # EV: an engine that already prices EV passes it in; otherwise, once a
        # setup has real history, its CONSERVATIVE empirical win share (the
        # worse of full-history and held-out) stands in as the calibrated
        # probability — never the score, which is not a probability. Costs are
        # charged as the half-spread out plus the fee allowance.
        ask = float(features.get("ask", 0.0))
        history = self._setup_stats(category, state, _band(score))
        if ev_per_dollar is None and history is not None \
                and history["n"] >= cfg.min_setup_sample and 0 < ask < 1:
            p = min(history["win"], history["win_recent"])
            ev_per_dollar = (p - ask) / ask - (spread / ask) * 0.5
            evidence["evSource"] = "empirical"
        if ev_per_dollar is not None and ev_per_dollar < cfg.min_expected_value:
            reasons.append(
                f"EV {ev_per_dollar:+.3f}/$ under the "
                f"{cfg.min_expected_value:+.3f} minimum after costs")
        evidence["ev"] = ev_per_dollar

        contradictions = self._contradictions(features, wallet_net, spread,
                                              category, context, token_id)
        evidence["contradictions"] = contradictions
        if len(contradictions) >= cfg.contradiction_tolerance:
            reasons.append(
                f"{len(contradictions)} pieces of structure argue against "
                f"this trade (tolerance {cfg.contradiction_tolerance})")

        crowd = self._category_share(category, context)
        if crowd is not None and crowd > cfg.max_category_share:
            reasons.append(
                f"{crowd:.0%} of open exposure is already {category or '?'} - "
                "this does not improve the portfolio, it concentrates it")

        provisional = False
        history = self._setup_stats(category, state, _band(score))
        evidence["setupSample"] = history["n"] if history else 0
        if history is None or history["n"] < cfg.min_setup_sample:
            if self.live or cfg.empirical_strict_in_paper:
                reasons.append(
                    "no validated history for this setup "
                    f"({0 if history is None else history['n']} of "
                    f"{cfg.min_setup_sample} needed) - unproven setups do not "
                    "trade live money")
            else:
                provisional = True      # paper: learn, and say so
        else:
            evidence["setupWin"] = round(history["win"], 4)
            evidence["setupWinRecent"] = round(history["win_recent"], 4)
            if history["win"] < cfg.min_empirical_win:
                reasons.append(
                    f"setup wins {history['win']:.0%} historically, under the "
                    f"{cfg.min_empirical_win:.0%} bar")
            elif history["win"] - history["win_recent"] > cfg.max_validation_drop:
                reasons.append(
                    f"setup decayed out of sample: {history['win']:.0%} on the "
                    f"data it was graded on, {history['win_recent']:.0%} on "
                    "the newest slice - that gap is what overfitting looks like")

        return Verdict(allow=not reasons, provisional=provisional,
                       reasons=reasons, evidence=evidence)

    # -- pieces --------------------------------------------------------------

    @staticmethod
    def _contradictions(features: dict, wallet_net: float, spread: float,
                        category: str = "", context: Any = None,
                        token_id: str = "") -> list[str]:
        out: list[str] = []
        move = float(features.get("ms_chg_300s", 0.0))
        if float(features.get("ms_state", 0.0)) == 5.0:
            out.append("reversal state")
        if float(features.get("ms_liquidity_chg", 0.0)) < -0.3:
            out.append("book draining")
        imbalance = float(features.get("ms_imbalance", 0.0))
        if move != 0 and imbalance != 0 and move * imbalance < 0:
            out.append("flow against the move")
        if spread > 0.10:
            out.append("abnormal spread")
        if move != 0 and wallet_net != 0 and move * wallet_net < 0:
            out.append("ranked wallets on the other side")
        # Opposing cross-market movement: comparable markets (same category)
        # travelling firmly the other way is evidence this move is local noise
        # rather than the category's information.
        if move != 0 and category and context is not None:
            states = getattr(context, "market_state", None) or {}
            markets = getattr(context, "markets", {}) or {}
            peer_tokens = {t for m in markets.values()
                           if getattr(m, "category", "") == category
                           for t in getattr(m, "quotes", {})}
            peers = [float(s.get("ms_chg_300s", 0.0))
                     for token, s in states.items()
                     if token != token_id and token in peer_tokens and abs(
                         float(s.get("ms_chg_300s", 0.0))) >= 0.01]
            if len(peers) >= 3:
                mean_peer = sum(peers) / len(peers)
                if mean_peer * move < 0 and abs(mean_peer) >= 0.02:
                    out.append("category peers moving the other way")
        return out

    @staticmethod
    def _category_share(category: str, context: Any) -> Optional[float]:
        positions = getattr(context, "positions", None) or []
        if not positions or not category:
            return None
        markets = getattr(context, "markets", {}) or {}
        total = same = 0.0
        for position in positions:
            value = position.size * (position.cur_price or position.avg_price)
            total += value
            market = markets.get(position.market_id)
            if market is not None and getattr(market, "category", "") == category:
                same += value
        if total <= 0:
            return None
        return same / total

    def _setup_stats(self, category: str, state: float,
                     band: str) -> Optional[dict]:
        """Win statistics for comparable closed setups, split in time.

        The split is the anti-overfitting rule made mechanical: the newest
        ``validation_fraction`` of the setup's closed history is held out, and
        confidence is only honest if that unseen slice agrees with the rest.
        """
        setups = self._load_setups()
        return setups.get((category or "", float(state), band))

    def _load_setups(self) -> dict:
        if self.journal is None:
            return {}
        now = time.time()
        if now - self._setups_at < _SETUP_CACHE_SECONDS:
            return self._setups
        self._setups_at = now
        try:
            rows = self.journal.query(
                """SELECT d.score, d.category, d.features, l.realized_pnl,
                          l.exit_ts
                     FROM lifecycles l
                     JOIN decisions d ON d.id = l.entry_decision_id
                    WHERE l.status='CLOSED' ORDER BY l.exit_ts""")
        except Exception:                                 # noqa: BLE001
            return self._setups
        grouped: dict[tuple, list] = {}
        for row in rows:
            try:
                feats = json.loads(row.get("features") or "{}")
            except (TypeError, ValueError):
                feats = {}
            key = (str(row.get("category") or ""),
                   float(feats.get("ms_state", 0.0)),
                   _band(float(row.get("score") or 0.0)))
            grouped.setdefault(key, []).append(
                1.0 if (row.get("realized_pnl") or 0) > 0 else 0.0)
        out: dict[tuple, dict] = {}
        for key, wins in grouped.items():
            n = len(wins)
            cut = max(1, int(n * (1.0 - self.cfg.validation_fraction)))
            older, newer = wins[:cut], wins[cut:]
            out[key] = {
                "n": n,
                "win": sum(wins) / n,
                "win_recent": (sum(newer) / len(newer)) if newer
                else (sum(older) / len(older)),
            }
        self._setups = out
        return out
