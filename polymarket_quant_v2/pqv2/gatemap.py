"""WHO OWNS EACH GATE — the map that decides what may block what.

The master prompt's central architectural rule is that Strategy B must not be
silently blocked by Strategy A's filters, and that only genuine GLOBAL SAFETY
constraints may block both routes. That rule is unenforceable without an
explicit answer to "which layer does this rejection belong to", so this module
is that answer, written down once, in one place, with the evidence for each
call.

Every gate in the existing engine (`polymarket-quant-bridge`) is listed here
with:

    owner    — STRATEGY_A | STRATEGY_B | GLOBAL_SAFETY | PORTFOLIO | EXECUTION
    source   — the file:line that emits it, so a claim here is checkable
    pattern  — how the rejection reads in the journal
    evidence — WHY it is classified that way

Nothing here changes any threshold. It is a classification, and its only
executable consequence is :func:`blocks_route` — which the V2 router consults
to decide whether a given gate is entitled to stop a Strategy B signal.

**The classification rule, applied consistently.** A gate is GLOBAL_SAFETY only
if violating it would harm the account regardless of which strategy asked for
the trade — no cash, no book to trade into, market already resolved. A gate
that encodes an *opinion about edge* belongs to the strategy that holds the
opinion, however sensible it looks. "Market state 0 is not an entry state" is
an opinion about edge. "Cash is below the reserve" is not.

That distinction is the whole patch. On the measured build it is the difference
between a system that took 0 trades in 92 hours and one that can act on the
two independently validated strategies it already owns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# -- owners -----------------------------------------------------------------

STRATEGY_A = "STRATEGY_A"
STRATEGY_B = "STRATEGY_B"
GLOBAL_SAFETY = "GLOBAL_SAFETY"
PORTFOLIO = "PORTFOLIO"
EXECUTION = "EXECUTION"

OWNERS = (STRATEGY_A, STRATEGY_B, GLOBAL_SAFETY, PORTFOLIO, EXECUTION)

# Routes a signal can travel.
ROUTE_A = "A"
ROUTE_B = "B"


@dataclass(frozen=True)
class Gate:
    """One place a trade can be refused, and who is entitled to refuse it."""

    key: str
    owner: str
    source: str
    pattern: str
    summary: str
    evidence: str
    # Set only where the gate was measured to be the binding constraint.
    measured: str = ""

    def blocks(self, route: str) -> bool:
        """May this gate stop a signal travelling `route`?

        GLOBAL_SAFETY, PORTFOLIO and EXECUTION bind both routes: they are
        statements about the account and the venue, not about edge. A strategy
        gate binds only its own route — which is the rule the prompt calls
        non-negotiable, expressed as one line of code rather than as a
        convention nobody can check.
        """
        if self.owner in (GLOBAL_SAFETY, PORTFOLIO, EXECUTION):
            return True
        if self.owner == STRATEGY_A:
            return route == ROUTE_A
        if self.owner == STRATEGY_B:
            return route == ROUTE_B
        return True


# ---------------------------------------------------------------------------
# The map. Sources are file:line in polymarket-quant-bridge at build 71.
# ---------------------------------------------------------------------------

GATES: tuple[Gate, ...] = (

    # -- the one that mattered ---------------------------------------------
    Gate(
        key="learning_mode",
        owner=STRATEGY_A,
        source="pqb/bridge/lean_engine.py:223 (_entry_block_reason)",
        pattern="Learning mode: no validated strategies yet",
        summary="No entries at all until Strategy A's discovery validates a "
                "rule.",
        evidence=(
            "Reads `self.trading_strategies`, which is Strategy A's own "
            "discovered-rule set loaded from strategies.json. It is a "
            "statement about whether STRATEGY A has an edge yet. It says "
            "nothing about the account, the venue or the market, so it cannot "
            "be global safety — and applying it to a route that has its own "
            "independently validated strategies is the exact failure the "
            "master prompt names: Strategy B vetoed by Strategy A's state."),
        measured=(
            "40,820 of 40,820 decisions over 92.3 hours. 100% of the account's "
            "inactivity. Strategy A library: 170 rejected / 49 validating / "
            "13 new / 2 quarantined / 0 VALIDATED."),
    ),

    # -- Strategy A's edge opinions ----------------------------------------
    Gate(
        key="hc_market_state",
        owner=STRATEGY_A,
        source="pqb/decision/high_confidence.py:96",
        pattern="market state N is not an entry state",
        summary="The Market-State classifier's label must be one of the "
                "configured entry states.",
        evidence=(
            "`allowed_states` is a tuned parameter of the high-confidence "
            "filter, which `lean_engine._entry_filter` arms over LEAN entry "
            "candidates only. It encodes Strategy A's view of when a move is "
            "worth entering. A wallet-copy strategy validated on its own "
            "out-of-sample evidence has no obligation to share that view."),
    ),
    Gate(
        key="hc_depth",
        owner=STRATEGY_A,
        source="pqb/decision/high_confidence.py:106",
        pattern="depth $N is under Nx the stake",
        summary="Visible depth must be >= min_depth_x_stake x the stake.",
        evidence=(
            "Sits in the same filter and is expressed as a multiple of "
            "Strategy A's own sizing. It is close to an execution concern, and "
            "the honest split is: 'can this size be filled at all' is "
            "EXECUTION and is enforced there (see `exec_liquidity`); 'is there "
            "3x headroom' is a quality preference and belongs to A."),
    ),
    Gate(
        key="hc_filter_generic",
        owner=STRATEGY_A,
        source="pqb/bridge/baseline_engine.py:528",
        pattern="High-confidence filter: ...",
        summary="Any other high-confidence rejection.",
        evidence="Armed only by `lean_engine._entry_filter`; the base engine "
                 "returns None. Strategy A by construction.",
    ),
    Gate(
        key="min_score",
        owner=STRATEGY_A,
        source="pqb/bridge/baseline_engine.py:408",
        pattern="none reached the N entry score",
        summary="Blended market/wallet score below `entry.min_score`.",
        evidence="Strategy A's scoring function and its threshold.",
    ),
    Gate(
        key="price_band",
        owner=STRATEGY_A,
        source="pqb/bridge/baseline_engine.py:517 (_entry_gate)",
        pattern="price outside band",
        summary="Ask outside [entry.min_price, entry.max_price].",
        evidence=(
            "A preference, and demonstrably not a shared one: walletlab's two "
            "VALIDATED specs trade 0.70-0.98 and 0.50-0.98, and the first "
            "operates almost entirely above A's 0.95 ceiling. Applying A's "
            "band to B would silently delete most of B's edge."),
    ),
    Gate(
        key="max_spread",
        owner=STRATEGY_A,
        source="pqb/bridge/baseline_engine.py:519",
        pattern="spread too wide",
        summary="Quote spread above `entry.max_spread`.",
        evidence="A cost preference expressed as a fixed threshold. B prices "
                 "its own spread tolerance into its backtest.",
    ),
    Gate(
        key="require_wallet_signal",
        owner=STRATEGY_A,
        source="pqb/bridge/baseline_engine.py:405",
        pattern="noWalletSignal",
        summary="Entry requires a live wallet signal.",
        evidence="Strategy A's optional evidence requirement.",
    ),

    # -- genuine global safety ---------------------------------------------
    Gate(
        key="no_quote",
        owner=GLOBAL_SAFETY,
        source="pqb/bridge/baseline_engine.py:515,521",
        pattern="no ask | no live quote",
        summary="No live price to trade against.",
        evidence="There is no order to send. True for every route, every "
                 "strategy, every regime.",
    ),
    Gate(
        key="market_resolving",
        owner=GLOBAL_SAFETY,
        source="pqb/bridge/baseline_engine.py:524",
        pattern="already resolving",
        summary="Time to resolution <= 0.",
        evidence="Entering a market that has stopped trading is not a strategy "
                 "choice.",
    ),
    Gate(
        key="cash_reserve",
        owner=GLOBAL_SAFETY,
        source="pqb/riskpolicy.py:325 (evaluate)",
        pattern="cash $N is at or below the $N reserve",
        summary="Cash below the capital-preservation floor.",
        evidence="A property of the account. Preserved from the v1 surgical "
                 "risk patch unchanged.",
    ),
    Gate(
        key="halt_or_kill",
        owner=GLOBAL_SAFETY,
        source="pqb/runner.py:_gate_state",
        pattern="halt | kill_switch",
        summary="Reconciliation halt or the operator's kill switch.",
        evidence="The operator's switch and the do-not-trade-on-unknown-state "
                 "rule. Must bind everything.",
    ),
    Gate(
        key="drawdown_halt",
        owner=GLOBAL_SAFETY,
        source="pqb/riskpolicy.py:356",
        pattern="account is N% below its peak",
        summary="Entry halt at the configured drawdown.",
        evidence="Account-level, route-independent, and the last line of the "
                 "v1 patch. Binds both.",
    ),

    # -- portfolio ----------------------------------------------------------
    Gate(
        key="position_cap",
        owner=PORTFOLIO,
        source="pqb/bridge/baseline_engine.py:364",
        pattern="At the N-position limit",
        summary="Open-position ceiling.",
        evidence="Capital management, not strategy quality. The prompt "
                 "requires these be recorded separately so a portfolio "
                 "rejection is never mistaken for a bad signal.",
    ),
    Gate(
        key="cluster_cap",
        owner=PORTFOLIO,
        source="pqb/riskpolicy.py:343",
        pattern="correlated exposure in ... is at the $N cap",
        summary="One market/thesis/category holding too much.",
        evidence="Correlated-exposure control from the v1 patch. Applies to "
                 "the book, so both routes.",
    ),
    Gate(
        key="total_exposure",
        owner=PORTFOLIO,
        source="pqb/riskpolicy.py:333",
        pattern="open exposure $N has reached the $N ceiling",
        summary="Total open exposure ceiling.",
        evidence=(
            "Binds on the mark-to-market of the whole book as a fraction of "
            "equity, so it is a statement about how much of the account is "
            "at risk at once — a quantity neither strategy can see from "
            "inside its own signal. Both routes spend the same balance, so "
            "both must respect the same ceiling."),
    ),
    Gate(
        key="same_market",
        owner=PORTFOLIO,
        source="pqb/bridge/baseline_engine.py:481 (_allocate)",
        pattern="sameMarket",
        summary="Already holding this market.",
        evidence="Holding both outcomes of a binary market is a locked-in "
                 "loss of the spread. Structural, both routes.",
    ),

    # -- execution ----------------------------------------------------------
    Gate(
        key="exec_liquidity",
        owner=EXECUTION,
        source="pqb/adapters/execution_adapter.py",
        pattern="insufficient liquidity | partial fill",
        summary="The book cannot fill the requested size.",
        evidence="A fact about the venue at the moment of sending.",
    ),
    Gate(
        key="min_order",
        owner=EXECUTION,
        source="pqb/bridge/baseline_engine.py:484",
        pattern="unfunded | below the $N minimum trade size",
        summary="Stake below the venue/progression minimum.",
        evidence="The exchange will reject it. Route-independent.",
    ),
    Gate(
        key="fee_drag",
        owner=EXECUTION,
        source="pqb/bridge/baseline_engine.py:496",
        pattern="feeTooHigh",
        summary="Round-trip fee exceeds max_fee_fraction of the stake.",
        evidence=(
            "A trade too small to outrun its own fee loses money whoever "
            "asked for it. Genuinely route-independent — but note it is a "
            "function of STAKE, so a route that sizes differently meets it "
            "differently, which is why B carries its own sizing."),
    ),
)


GATES_BY_KEY = {g.key: g for g in GATES}


def gates_for(owner: str) -> list:
    return [g for g in GATES if g.owner == owner]


def blocks_route(gate_key: str, route: str) -> bool:
    """May `gate_key` stop a signal on `route`? Unknown gates block.

    Unknown-blocks is the safe default and also the honest one: a rejection
    this map has never seen is a rejection nobody has classified, and the
    correct response is to stop and add it here rather than to wave it through
    on the grounds that it is unrecognised.
    """
    gate = GATES_BY_KEY.get(gate_key)
    return gate.blocks(route) if gate is not None else True


def classify(reason: str) -> str:
    """Best-effort gate key for a free-text rejection from the journal.

    Substring matching against the recorded patterns. Deliberately returns
    ``"unclassified"`` rather than guessing: the audit reports unclassified
    rejections as a number the operator is meant to drive to zero, and a
    fuzzy match that quietly absorbed them would hide exactly the gates
    nobody has thought about yet.
    """
    text = (reason or "").lower()
    if not text:
        return "unclassified"
    for key, needles in _MATCHERS:
        if any(n in text for n in needles):
            return key
    return "unclassified"


_MATCHERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("learning_mode", ("learning mode",)),
    ("hc_market_state", ("is not an entry state",)),
    ("hc_depth", ("depth $", " is under ")),
    ("hc_filter_generic", ("high-confidence filter",)),
    ("min_score", ("entry score", "below the score")),
    ("price_band", ("price outside band",)),
    ("max_spread", ("spread too wide",)),
    ("require_wallet_signal", ("no wallet signal",)),
    ("no_quote", ("no ask", "no live quote")),
    ("market_resolving", ("already resolving",)),
    ("cash_reserve", ("reserve",)),
    ("halt_or_kill", ("halt", "kill switch", "flatten")),
    ("drawdown_halt", ("below its peak", "drawdown")),
    ("position_cap", ("position limit", "-position limit")),
    ("cluster_cap", ("correlated exposure",)),
    ("total_exposure", ("open exposure",)),
    ("same_market", ("same market",)),
    ("exec_liquidity", ("liquidity", "partial fill")),
    ("min_order", ("minimum trade size", "unfunded")),
    ("fee_drag", ("fee",)),
)


def summary() -> dict:
    """The map as data, for the dashboard and the audit."""
    out: dict = {"owners": {}, "gates": []}
    for owner in OWNERS:
        rows = gates_for(owner)
        out["owners"][owner] = {
            "count": len(rows),
            "blocksBothRoutes": owner in (GLOBAL_SAFETY, PORTFOLIO, EXECUTION),
            "keys": [g.key for g in rows],
        }
    for gate in GATES:
        out["gates"].append({
            "key": gate.key, "owner": gate.owner, "source": gate.source,
            "pattern": gate.pattern, "summary": gate.summary,
            "evidence": gate.evidence, "measured": gate.measured,
            "blocksA": gate.blocks(ROUTE_A), "blocksB": gate.blocks(ROUTE_B),
        })
    return out
