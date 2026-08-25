"""Backtesting: gross evaluation, and the $100 capital simulation.

Two distinct things live here and they must not be confused, so they are two
functions with two result types:

**`evaluate`** measures a hypothesis's edge per unit staked, ignoring capital.
Every admitted observation contributes one return. This answers "does this rule
select profitable trades?" and is the right input to a significance test,
because it is not distorted by the order trades happened to arrive in.

**`capital_test`** answers a completely different question: "starting from
$100, with venue minimums, position caps, correlated-exposure limits and one
bankroll, what would this actually have produced?" It walks the admitted
observations in time order and skips any trade the account could not fund at
that moment.

The gap between them is the point. A rule with a strong per-trade edge can be
worth almost nothing at $100 because most of its signals are unaffordable, and
reporting only the first number is the most flattering lie available. Both are
reported, always, side by side.

Payoff is exact, not modelled. A hold-to-resolution trade bought at `p` pays
`resolution - p` with `resolution` in {0, 1}. No order book is needed for that,
which is why this substrate is evaluable at all — see `docs/ENGINE-LIMITS.md`
for what is NOT evaluable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..portfolio.capital import Account, CapitalEngine, Exposure
from .baseline import MatchedBaseline, build as build_matched
from .hypothesis import Hypothesis, admit_mask
from .matrix import Matrix
from . import stats


@dataclass
class Evaluation:
    """Gross, capital-free evaluation of a hypothesis over a window."""

    n: int = 0
    n_comparable: int = 0
    returns: list = field(default_factory=list)
    excess: list = field(default_factory=list)
    markets: int = 0
    wallets: int = 0
    expectancy: float = 0.0
    win_rate: float = 0.0
    profit_factor: float | None = 0.0
    max_drawdown: float = 0.0
    concentration: float = 0.0
    p_value: float = 1.0
    t_stat: float = 0.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    bootstrap_positive: float = 0.0
    baseline_expectancy: float = 0.0
    alpha_vs_baseline: float = 0.0
    ts_from: int = 0
    ts_to: int = 0

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()
                if k not in ("returns", "excess")}


def evaluate(m: Matrix, h: Hypothesis, st: Settings, *, lo: int, hi: int,
             matched: MatchedBaseline | None = None,
             baseline_returns: list | None = None,
             with_stats: bool = True) -> Evaluation:
    """Score one hypothesis over rows [lo, hi).

    **Significance is tested on the MATCHED EXCESS series, not on raw returns.**
    That is the single most important line in this file. Raw returns are
    `(resolution - p)/p`, so a winning longshot pays +19 and a winning
    favourite pays +0.11; the mean over everything is dominated by longshots and
    is strongly negative. Against that, any rule that merely avoids longshots
    scores enormous "alpha" — the first run of this system reported +0.50 for
    `price >= 0.53`, which is a price preference, not an edge.

    Comparing each observation only against others in the same price band and
    week removes that. See `baseline.py`.
    """
    idx = admit_mask(m, h, lo, hi)
    ev = Evaluation(n=len(idx), ts_from=m.ts[lo] if lo < len(m.ts) else 0,
                    ts_to=m.ts[hi - 1] if 0 < hi <= len(m.ts) else 0)
    if not idx:
        return ev

    mb = matched if matched is not None else build_matched(m, st, lo, hi)

    cost = 1.0 + (st.costs.slippage_bps + st.costs.fee_bps) / 10_000.0
    rets, excess, keys, mkts, wals = [], [], [], set(), set()
    for i in idx:
        p = m.cols["price"][i] * cost
        if not (0 < p < 1):
            continue
        rets.append((m.resolution[i] - p) / p)
        e = mb.excess(i)
        if e is not None:
            excess.append(e)
            keys.append(m.market_id[i])
        mkts.add(m.market_id[i])
        wals.add(m.wallet[i])

    ev.returns = rets
    ev.excess = excess
    ev.n = len(rets)
    ev.n_comparable = len(excess)
    ev.markets = len(mkts)
    ev.wallets = len(wals)
    if not rets:
        return ev

    s = stats.summarize(rets)
    ev.expectancy = s["expectancy"]
    ev.win_rate = s["win_rate"]
    ev.profit_factor = s["profit_factor"]
    ev.max_drawdown = s["max_drawdown"]

    if excess:
        ev.baseline_expectancy = round(ev.expectancy - stats.mean(excess), 6)
        ev.alpha_vs_baseline = round(stats.mean(excess), 6)
        # Concentration is measured on the EXCESS, because the question is
        # whether the edge is one market, not whether the profit is.
        ev.concentration = round(stats.concentration(excess, keys), 5)
    elif baseline_returns:
        ev.baseline_expectancy = round(stats.mean(baseline_returns), 6)
        ev.alpha_vs_baseline = round(ev.expectancy - ev.baseline_expectancy, 6)

    if with_stats and excess:
        t, _ = stats.t_stat(excess)
        ev.t_stat = round(t, 4)
        ev.p_value = round(stats.p_value_one_sided(excess), 8)
        lo_ci, hi_ci, pos = stats.block_bootstrap_ci(
            excess, draws=st.research.bootstrap_draws, seed=st.research.seed)
        ev.ci_low, ev.ci_high = round(lo_ci, 6), round(hi_ci, 6)
        ev.bootstrap_positive = round(pos, 4)
    return ev


# ---------------------------------------------------------------------------
# The $100 capital test
# ---------------------------------------------------------------------------

@dataclass
class CapitalTest:
    """What the strategy would actually have done with one real bankroll."""

    starting_capital: float = 0.0
    ending_capital: float = 0.0
    total_return: float = 0.0
    trades: int = 0
    signals: int = 0
    skipped_capital: int = 0
    skipped_liquidity: int = 0
    skipped_exposure: int = 0
    skipped_other: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    profit_factor: float | None = 0.0
    max_drawdown: float = 0.0
    capital_utilisation: float = 0.0
    largest_position: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    average_trade: float = 0.0
    fees_paid: float = 0.0
    slippage_paid: float = 0.0
    equity_curve: list = field(default_factory=list)
    skip_reasons: dict = field(default_factory=dict)
    # Horizon accounting. Without these, every strategy on a short window
    # reports the same trade count (the concurrent-position cap) and the
    # capital test looks broken when it is merely constrained.
    window_days: float = 0.0
    signal_span_days: float = 0.0
    median_hold_days: float = 0.0
    still_open_at_end: int = 0
    max_possible_trades: int = 0
    horizon_limited: bool = False
    reliable: bool = True
    hold_model: str = "DATA"          # DATA | MODELLED
    settlement_clock: dict = field(default_factory=dict)
    note: str = ""

    @property
    def fill_rate(self) -> float:
        return self.trades / self.signals if self.signals else 0.0

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "equity_curve"}
        d["fill_rate"] = round(self.fill_rate, 4)
        d["equity_curve_points"] = len(self.equity_curve)
        return d


def settlement_clock_quality(m: Matrix, lo: int, hi: int) -> dict:
    """Is the settlement clock usable for a capital simulation?

    MEASURED, not assumed. On the current V1 database it is not, and the
    failure is total rather than marginal: `resolutions.settled_ts` is 0 in all
    8,116 rows, so V2 falls back to `resolutions.ts` — the moment V1 OBSERVED
    the resolution. V1 observed them all in one batch, so every position in a
    25.6-day window appears to settle inside the same 0.4 days, three days
    AFTER the tape ends.

    The consequence is not subtle. A hold-to-resolution simulation frees a
    position's capital at settlement; if nothing ever settles, the account
    fills its concurrent-position slots once and then skips every remaining
    signal forever. Every strategy then reports exactly `max_open_positions`
    trades, and the resulting return is a property of the data defect rather
    than of the strategy.

    So the capital test measures this first and refuses to present a return as
    a strategy result when the clock is degenerate.
    """
    if hi <= lo:
        return {"usable": False, "reason": "empty window"}
    entry = [m.ts[i] for i in range(lo, hi)]
    settle = [m.ts[i] + int(m.cols["secs_to_settle"][i]) for i in range(lo, hi)]
    e_span = max(entry) - min(entry)
    s_span = max(settle) - min(settle)
    ratio = (s_span / e_span) if e_span else 0.0
    past_end = min(settle) > m.ts[-1]
    usable = ratio >= 0.25 and not past_end
    return {
        "usable": usable, "ratio": round(ratio, 4),
        "entry_span_days": round(e_span / 86400.0, 2),
        "settle_span_days": round(s_span / 86400.0, 2),
        "all_settle_after_tape_end": past_end,
        "reason": "" if usable else (
            f"settlement timestamps are degenerate: entries span "
            f"{e_span / 86400:.1f} days but settlements span only "
            f"{s_span / 86400:.1f} days"
            + (" and all fall after the tape ends" if past_end else "")
            + ". resolutions.settled_ts is unpopulated, so settlement time is "
              "really 'when V1 noticed', which it did in one batch. Fix with "
              "`pqv3 collect --backfill-settled`."),
    }


def capital_test(m: Matrix, h: Hypothesis, st: Settings, *, lo: int,
                 hi: int, hold_secs: int = 0) -> CapitalTest:
    """Walk admitted signals in time order against one bankroll.

    Positions are held to resolution, so capital is committed at entry and
    returned at `settled_ts`. A signal arriving while the account is fully
    deployed is SKIPPED and counted — the count is the honest measure of how
    much of a strategy's paper edge a small account can actually reach.
    """
    idx = admit_mask(m, h, lo, hi)
    cap = st.capital
    ct = CapitalTest(starting_capital=cap.starting_capital,
                     ending_capital=cap.starting_capital,
                     signals=len(idx))
    if not idx:
        return ct

    clock = settlement_clock_quality(m, lo, hi)
    ct.settlement_clock = clock
    # When the clock is degenerate, hold for an EXPLICIT modelled period rather
    # than a fabricated one. The result is labelled MODELLED everywhere it is
    # reported, and the validation ladder refuses to treat it as evidence.
    if not clock["usable"] and not hold_secs:
        hold_secs = st.research.modelled_hold_secs
        ct.hold_model = "MODELLED"
        ct.reliable = False
    elif hold_secs:
        ct.hold_model = "MODELLED"
        ct.reliable = False
    else:
        ct.hold_model = "DATA"
        ct.reliable = True

    engine = CapitalEngine(st)
    acct = Account(starting_capital=cap.starting_capital,
                   cash=cap.starting_capital,
                   peak_equity=cap.starting_capital)

    # Open positions, released at settlement. Settlement time is not in the
    # matrix, so it is reconstructed from `secs_to_settle`, which V2 computed
    # causally at entry. Where it is unknown (-1) the position is released
    # after a conservative 7 days rather than never — never-releasing would
    # let one unresolvable trade freeze the account and understate everything
    # after it.
    import heapq
    pending: list = []
    seq = 0
    trade_pnls: list = []
    deployed_sum = 0.0
    steps = 0

    for i in idx:
        now = m.ts[i]

        while pending and pending[0][0] <= now:
            _, _, sz, ret, ck = heapq.heappop(pending)
            pnl = sz * ret
            acct.cash += sz + pnl
            acct.realized_pnl += pnl
            acct.open_positions -= 1
            acct.exposure.gross = max(0.0, acct.exposure.gross - sz)
            if ck:
                left = acct.exposure.by_correlation.get(ck, 0.0) - sz
                if left > 1e-9:
                    acct.exposure.by_correlation[ck] = left
                    acct.exposure.by_market[ck] = left
                else:
                    acct.exposure.by_correlation.pop(ck, None)
                    acct.exposure.by_market.pop(ck, None)
            trade_pnls.append(pnl)
            ct.largest_win = max(ct.largest_win, pnl)
            ct.largest_loss = min(ct.largest_loss, pnl)

        acct.position_value = sum(p[2] for p in pending)
        acct.peak_equity = max(acct.peak_equity, acct.equity)
        ct.equity_curve.append((now, round(acct.equity, 4)))
        deployed_sum += acct.exposure.gross
        steps += 1

        if acct.drawdown >= cap.hard_stop_drawdown:
            ct.skip_reasons["hard_stop_drawdown"] = \
                ct.skip_reasons.get("hard_stop_drawdown", 0) + 1
            ct.skipped_other += 1
            continue

        price = m.cols["price"][i]
        # Liquidity proxy from the tape: the notional that actually printed on
        # this trade. Never assumed larger, because nothing measured it.
        liquidity = max(m.cols["notional"][i], 0.0)
        ck = m.market_id[i] or m.token_id[i]

        r = engine.size(account=acct, probability=_prior(m, i),
                        signal_price=price, available_liquidity=liquidity,
                        confidence=1.0, correlation_key=ck)
        if not r.ok:
            f = r.feasibility.value
            ct.skip_reasons[f] = ct.skip_reasons.get(f, 0) + 1
            if f in ("CAPITAL_INFEASIBLE", "NO_CASH"):
                ct.skipped_capital += 1
            elif f == "LIQUIDITY_INFEASIBLE":
                ct.skipped_liquidity += 1
            elif f in ("EXPOSURE_LIMIT", "POSITION_LIMIT"):
                ct.skipped_exposure += 1
            else:
                ct.skipped_other += 1
            continue

        gross_ret = (m.resolution[i] - r.entry_price) / r.entry_price
        acct.cash -= r.size_usdc
        acct.open_positions += 1
        acct.exposure.gross += r.size_usdc
        acct.exposure.by_correlation[ck] = \
            acct.exposure.by_correlation.get(ck, 0.0) + r.size_usdc
        acct.exposure.by_market[ck] = acct.exposure.by_correlation[ck]
        ct.trades += 1
        ct.largest_position = max(ct.largest_position, r.size_usdc)
        ct.fees_paid += r.fees
        ct.slippage_paid += abs(r.slippage_cost)

        if hold_secs:
            settle = now + hold_secs
        else:
            secs = m.cols["secs_to_settle"][i]
            settle = now + (int(secs) if secs and secs > 0 else 7 * 86_400)
        seq += 1
        heapq.heappush(pending, (settle, seq, r.size_usdc, gross_ret, ck))

    # Release everything still open at the end of the window. These are
    # counted: a strategy whose positions mostly had not resolved by the end of
    # the test has been measured over less time than it looks.
    still_open = len(pending)
    while pending:
        _, _, sz, ret, _ = heapq.heappop(pending)
        pnl = sz * ret
        acct.cash += sz + pnl
        acct.realized_pnl += pnl
        trade_pnls.append(pnl)
        ct.largest_win = max(ct.largest_win, pnl)
        ct.largest_loss = min(ct.largest_loss, pnl)

    ct.still_open_at_end = still_open
    acct.position_value = 0.0
    ct.ending_capital = round(acct.equity, 4)
    ct.total_return = round(
        (ct.ending_capital - ct.starting_capital) / ct.starting_capital, 6) \
        if ct.starting_capital else 0.0
    if trade_pnls:
        wins = [p for p in trade_pnls if p > 0]
        ct.win_rate = round(len(wins) / len(trade_pnls), 5)
        ct.expectancy = round(sum(trade_pnls) / len(trade_pnls), 6)
        ct.average_trade = ct.expectancy
        pf = stats.profit_factor(trade_pnls)
        ct.profit_factor = None if pf == float("inf") else round(pf, 4)
    curve = [e for _, e in ct.equity_curve] or [ct.starting_capital]
    from ..accel import default as accel_default
    ct.max_drawdown = round(accel_default().call("max_drawdown", curve), 5)
    ct.capital_utilisation = round(
        deployed_sum / steps / cap.starting_capital, 4) if steps else 0.0
    ct.fees_paid = round(ct.fees_paid, 4)
    ct.slippage_paid = round(ct.slippage_paid, 4)
    ct.largest_position = round(ct.largest_position, 4)
    ct.largest_win = round(ct.largest_win, 4)
    ct.largest_loss = round(ct.largest_loss, 4)

    # -- horizon accounting ------------------------------------------------
    if idx:
        span = m.ts[idx[-1]] - m.ts[idx[0]]
        ct.signal_span_days = round(span / 86400.0, 2)
        ct.window_days = round((m.ts[min(hi, len(m.ts)) - 1] - m.ts[lo])
                               / 86400.0, 2) if hi > lo else 0.0
        holds = sorted(v for v in
                       (m.cols["secs_to_settle"][i] for i in idx) if v > 0)
        ct.median_hold_days = round(
            holds[len(holds) // 2] / 86400.0, 2) if holds else 0.0
        # How many trades a bankroll holding to resolution could POSSIBLY make:
        # concurrent slots x the number of holding periods that fit.
        cycles = (ct.signal_span_days / ct.median_hold_days)             if ct.median_hold_days > 0 else 1.0
        ct.max_possible_trades = max(
            1, int(cap.max_open_positions * max(cycles, 1.0)))
        ct.horizon_limited = (
            ct.trades >= cap.max_open_positions
            and ct.skip_reasons.get("POSITION_LIMIT", 0) > ct.trades)
        if ct.horizon_limited:
            ct.note = (
                f"HORIZON-LIMITED, not a strategy result. Signals span "
                f"{ct.signal_span_days:.1f} days and positions are held "
                f"{ct.median_hold_days:.1f} days to resolution, so a "
                f"${cap.starting_capital:.0f} account with "
                f"{cap.max_open_positions} concurrent slots could place at "
                f"most ~{ct.max_possible_trades} trades however good the rule "
                f"is. {ct.skip_reasons.get('POSITION_LIMIT', 0)} signals were "
                f"skipped for want of a free slot, and {ct.still_open_at_end} "
                f"position(s) had not resolved when the window ended. Compare "
                f"strategies on out-of-sample expectancy, not on this return.")
        elif ct.trades < 20:
            ct.note = (f"only {ct.trades} trades were funded out of "
                       f"{ct.signals} signals; this return is a small sample")
    if not ct.reliable:
        ct.note = (
            f"MODELLED HOLD ({hold_secs / 86400:.1f} days), not measured. "
            + ct.settlement_clock.get("reason", "")
            + " Every number in this capital test therefore rests on an "
              "assumed holding period and must not be quoted as a backtest "
              "result. Out-of-sample expectancy is unaffected: it depends only "
              "on entry price and outcome, neither of which needs a clock."
            + ((" " + ct.note) if ct.note else ""))
    return ct


def _prior(m: Matrix, i: int) -> float:
    """Probability estimate used for Kelly sizing inside the capital test.

    Deliberately the market price, NOT the realised outcome and not a fitted
    value. Using anything the strategy learned from this same window would let
    sizing peek at the answer, and the capital test would then measure a
    strategy nobody could have run.
    """
    p = m.cols["price"][i]
    return max(0.01, min(0.99, p))


def baseline_returns(m: Matrix, st: Settings, lo: int, hi: int,
                     stride: int = 1) -> list:
    """Return of trading EVERY observation in the window: the do-nothing-clever
    benchmark every hypothesis must beat."""
    cost = 1.0 + (st.costs.slippage_bps + st.costs.fee_bps) / 10_000.0
    out = []
    for i in range(lo, hi, stride):
        p = m.cols["price"][i] * cost
        if 0 < p < 1:
            out.append((m.resolution[i] - p) / p)
    return out
