"""Wallet DNA: behavioural fingerprints from settled evidence.

The control that makes every number in this file interpretable is
`alpha_vs_band`, and it is worth stating why before anything else.

This dataset has a large favourite–longshot bias. Measured over all settled
trades, buying anything in the 0.60–0.80 price band returns roughly +9 points
of expectancy while copying nobody at all. So a wallet that only ever buys
0.70 favourites will show a 70%+ win rate and a positive ROI **while having no
skill whatsoever**. Rank wallets by win rate and you will build a leaderboard
of people who like favourites, then discover forty "independent validated
strategies" that are all the same market-wide effect.

`alpha_vs_band` subtracts that effect: it compares each wallet's realised
return to the return of every *other* wallet trading the same price band in the
same week. What survives is the part attributable to the wallet.

Second control: **outcomes fold in at settlement, never at entry.** A wallet's
statistics at time T contain only trades that had resolved by T. That is
enforced by a heap in the streaming pass, not by a promise, and it is why the
numbers here are usable as inputs to a point-in-time decision.

Known limitation, stated rather than buried: on the current V1 database
`resolutions.settled_ts` is 0 in all 8,116 rows, so settlement time falls back
to observation time. That can only *delay* information, never advance it — so
it is safe — but it is blunt. `ingest/settled_ts.py` is the fix; until it has
run, `pit_evidence_share` below reports how much point-in-time track record
actually existed, and it is close to zero.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import defaultdict

from ..config import Settings
from ..core.canon import WalletDNA
from ..core.source import HistoricalSource

BANDS = ((0.02, 0.20), (0.20, 0.35), (0.35, 0.50),
         (0.50, 0.65), (0.65, 0.80), (0.80, 0.98))


def _band(price: float) -> tuple:
    for lo, hi in BANDS:
        if lo <= price < hi:
            return (lo, hi)
    return BANDS[-1]


def _week(ts: int) -> int:
    return ts // (7 * 86_400)


class WalletIntelligence:
    """Builds DNA for many wallets in one streaming pass over the tape.

    One pass, not one per wallet: the market-wide band/week baselines needed
    for the alpha control require every wallet's trades anyway, so computing
    them per wallet would re-read the same 878k rows once per wallet.
    """

    def __init__(self, st: Settings, source: HistoricalSource | None = None) -> None:
        self.st = st
        self.source = source or HistoricalSource(st)

    def build(self, wallets: list[str] | None = None, *,
              min_trades: int = 60, max_wallets: int = 200,
              as_of: int = 0) -> dict:
        """Return {wallet: WalletDNA}. Empty dict if there is no data."""
        if not self.source.available:
            return {}
        if wallets is None:
            wallets = [w for w, _ in self.source.candidate_wallets()][:max_wallets]
        wanted = set(wallets)
        if not wanted:
            return {}

        # Pass state
        trades: dict = defaultdict(list)
        # Market-wide baselines, accumulated from EVERY wallet including ones we
        # are not profiling — the baseline must not be contaminated by only the
        # wallets we happened to select.
        band_week_ret: dict = defaultdict(list)

        for row in self.source.iter_settled(as_of=as_of):
            price = float(row["price"])
            res = float(row["resolution"])
            ret = (res - price) / price if price > 0 else 0.0
            key = (_band(price), _week(int(row["ts"])))
            band_week_ret[key].append(ret)
            if row["wallet"] in wanted:
                trades[row["wallet"]].append(row)

        # Bucket sums, computed once for the whole pass and shared by every
        # wallet's alpha calculation. Recomputing them per wallet would repeat
        # a sum over tens of thousands of returns once per wallet per trade.
        bucket_totals = {k: sum(v) for k, v in band_week_ret.items()}

        out: dict = {}
        for w, rows in trades.items():
            if len(rows) < min_trades:
                continue
            out[w] = self._one(w, rows, band_week_ret, bucket_totals)
        return out

    # -- one wallet ---------------------------------------------------------
    def _one(self, wallet: str, rows: list, band_week_ret: dict,
             bucket_totals: dict | None = None) -> WalletDNA:
        rows.sort(key=lambda r: int(r["ts"]))
        d = WalletDNA(wallet=wallet, trades=len(rows),
                      first_ts=int(rows[0]["ts"]), last_ts=int(rows[-1]["ts"]))

        prices = [float(r["price"]) for r in rows]
        notionals = [float(r["usdc"] or 0.0) for r in rows]
        rets = [(float(r["resolution"]) - float(r["price"])) / float(r["price"])
                if float(r["price"]) > 0 else 0.0 for r in rows]
        wins = [r for r in rows if float(r["resolution"]) > 0.5]
        markets = {r["market_id"] for r in rows if r["market_id"]}
        d.markets = len(markets)

        # -- WHAT / WHERE
        d.preferred_price = round(statistics.median(prices), 4)
        bandmix: dict = defaultdict(int)
        for p in prices:
            bandmix[f"{_band(p)[0]:.2f}-{_band(p)[1]:.2f}"] += 1
        d.price_band_mix = {k: round(v / len(prices), 4)
                            for k, v in sorted(bandmix.items())}
        ttrs = [(int(r["settled_ts"]) - int(r["ts"])) / 3600.0
                for r in rows if int(r["settled_ts"] or 0) > int(r["ts"])]
        d.preferred_ttr_hours = round(statistics.median(ttrs), 2) if ttrs else 0.0

        # -- WHEN
        for r in rows:
            ts = int(r["ts"])
            d.hour_histogram[(ts // 3600) % 24] += 1
            d.dow_histogram[(ts // 86400 + 4) % 7] += 1        # epoch was a Thu
        gaps = [b - a for a, b in zip([int(r["ts"]) for r in rows],
                                      [int(r["ts"]) for r in rows[1:]])]
        if len(gaps) > 2:
            m = statistics.fmean(gaps)
            # CV of inter-arrival times. 1.0 is Poisson; above 1 is bursty,
            # which usually means the wallet reacts to events rather than
            # trading a schedule.
            d.burstiness = round(statistics.pstdev(gaps) / m, 4) if m > 0 else 0.0

        # -- HOW MUCH
        d.avg_notional = round(statistics.fmean(notionals), 4)
        d.median_notional = round(statistics.median(notionals), 4)
        d.max_notional = round(max(notionals), 4)
        d.notional_dispersion = round(
            statistics.pstdev(notionals) / d.avg_notional, 4) \
            if d.avg_notional > 0 else 0.0

        # -- HOW: scaling and repetition
        by_token: dict = defaultdict(list)
        for r in rows:
            by_token[r["token_id"]].append(r)
        multi = [v for v in by_token.values() if len(v) > 1]
        d.repeat_market_rate = round(len(multi) / max(len(by_token), 1), 4)
        d.scaling_behavior = self._scaling(multi)

        # -- HOW: directional style, measured against the tape the wallet
        # traded into. Requires per-token print history, so it is sampled
        # rather than computed for every trade: 60 samples is enough to
        # classify a style and cheap enough to run over 200 wallets.
        d.momentum_score, d.mean_reversion_score, d.directional_bias = \
            self._style(rows[:60])

        if ttrs:
            d.late_market_rate = round(
                sum(1 for t in ttrs if t < 6) / len(ttrs), 4)
            d.early_market_rate = round(
                sum(1 for t in ttrs if t > 24 * 7) / len(ttrs), 4)

        # -- PERFORMANCE
        d.win_rate = round(len(wins) / len(rows), 4)
        stake = sum(notionals) or 1e-9
        pnl = sum(n * r for n, r in zip(notionals, rets))
        d.roi = round(pnl / stake, 5)
        d.expectancy = round(statistics.fmean(rets), 5)
        gross_win = sum(n * r for n, r in zip(notionals, rets) if r > 0)
        gross_loss = -sum(n * r for n, r in zip(notionals, rets) if r < 0)
        d.profit_factor = round(gross_win / gross_loss, 4) if gross_loss > 0 \
            else (float("inf") if gross_win > 0 else 0.0)

        equity, peak, mdd = 0.0, 0.0, 0.0
        for n, r in zip(notionals, rets):
            equity += n * r
            peak = max(peak, equity)
            if peak > 0:
                mdd = max(mdd, (peak - equity) / peak)
        d.max_drawdown = round(mdd, 4)

        sd = statistics.pstdev(rets) if len(rets) > 1 else 0.0
        d.sharpe_like = round(d.expectancy / sd * math.sqrt(len(rets)), 4) \
            if sd > 0 else 0.0
        downside = [r for r in rets if r < 0]
        dsd = statistics.pstdev(downside) if len(downside) > 1 else 0.0
        d.sortino_like = round(d.expectancy / dsd * math.sqrt(len(rets)), 4) \
            if dsd > 0 else 0.0

        # Calibration: does the wallet's entry price predict its hit rate? A
        # well-calibrated trader buying at 0.70 wins ~70% of the time.
        d.calibration_error = round(
            abs(statistics.fmean(prices) - d.win_rate), 4)

        # -- THE CONTROL
        d.alpha_vs_band = self._alpha(rows, rets, band_week_ret,
                                     bucket_totals)

        d.evidence_quality = self._quality(d)
        d.notes = self._notes(d)
        return d

    # -- helpers ------------------------------------------------------------
    def _alpha(self, rows: list, rets: list, band_week_ret: dict,
               bucket_totals: dict | None = None) -> float:
        """Mean excess return over other wallets in the same band and week.

        The wallet's own trade is removed from the baseline before comparing —
        leaving it in would shrink the measured alpha toward zero for a
        prolific wallet, because it would be competing against itself.

        `bucket_totals` carries the precomputed sum of each bucket. Without it
        this recomputes `sum(pool)` inside the per-trade loop, which is O(n)
        inside an O(n) loop over buckets holding tens of thousands of returns.
        The profiler put 1.2s of an 11.6s DNA pass in that one `sum`.
        """
        totals = bucket_totals if bucket_totals is not None else {}
        excess = []
        for r, ret in zip(rows, rets):
            key = (_band(float(r["price"])), _week(int(r["ts"])))
            pool = band_week_ret.get(key) or []
            if len(pool) < 10:
                continue
            total = totals.get(key)
            if total is None:
                total = totals[key] = sum(pool)
            excess.append(ret - (total - ret) / (len(pool) - 1))
        return round(statistics.fmean(excess), 5) if len(excess) >= 20 else 0.0

    def _scaling(self, multi: list) -> str:
        if not multi:
            return "SINGLE"
        up = down = flat = 0
        for group in multi:
            group.sort(key=lambda r: int(r["ts"]))
            first, last = float(group[0]["price"]), float(group[-1]["price"])
            sizes = [float(g["usdc"] or 0) for g in group]
            if last > first + 0.02:
                up += 1
            elif last < first - 0.02:
                down += 1
            else:
                flat += 1
            del sizes
        total = up + down + flat
        if total == 0:
            return "SINGLE"
        if up / total > 0.5:
            return "PYRAMID"          # adds as price rises
        if down / total > 0.5:
            return "AVERAGE_DOWN"     # adds as price falls
        return "SCALE_IN"

    def _style(self, sample: list) -> tuple:
        """Momentum vs mean-reversion, from the pre-trade tape.

        Compares the token's move in the hour BEFORE each entry to the entry
        itself. Buying after a rise is momentum; buying after a fall is
        reversion. Uses only prints strictly before the trade, so it cannot see
        the trade's own effect.
        """
        mom = rev = 0
        for r in sample:
            ts = int(r["ts"])
            prev = self.source.prints(r["token_id"], ts - 1, lookback_secs=3600,
                                      limit=200)
            if len(prev) < 3:
                continue
            move = prev[-1][1] - prev[0][1]
            if move > 0.01:
                mom += 1
            elif move < -0.01:
                rev += 1
        total = mom + rev
        if total == 0:
            return 0.0, 0.0, 0.0
        return (round(mom / total, 4), round(rev / total, 4),
                round((mom - rev) / total, 4))

    def _quality(self, d: WalletDNA) -> str:
        """How much weight this fingerprint can bear.

        Deliberately conservative: a wallet with 200 trades across 5 markets is
        five observations of market selection, not two hundred.
        """
        if d.trades < 60 or d.markets < 10:
            return "INSUFFICIENT"
        span_days = (d.last_ts - d.first_ts) / 86400.0
        if span_days < 14:
            return "THIN"
        if d.trades >= 300 and d.markets >= 40 and span_days >= 45:
            return "STRONG"
        return "MODERATE"

    def _notes(self, d: WalletDNA) -> list:
        notes = []
        if abs(d.alpha_vs_band) < 0.005 and d.win_rate > 0.6:
            notes.append(
                f"win rate {d.win_rate:.0%} with alpha {d.alpha_vs_band:+.4f}: "
                f"this is the price band, not the wallet")
        if d.profit_factor == float("inf"):
            notes.append("no losing trades in the sample; treat as unmeasured "
                         "rather than perfect")
        if d.max_drawdown > 0.5:
            notes.append(f"drawdown reached {d.max_drawdown:.0%} of peak profit")
        if d.notional_dispersion > 2.0:
            notes.append("position sizes vary by more than 2x their mean; "
                         "copying notional would misrepresent conviction")
        if d.calibration_error > 0.15:
            notes.append(f"entry prices are miscalibrated by "
                         f"{d.calibration_error:.2f} against realised hit rate")
        return notes


def rank(dnas: dict, *, by: str = "alpha") -> list:
    """Leaderboards. Never by lifetime PnL alone.

    Lifetime PnL ranks by size of bankroll and length of history, which selects
    for whales and survivors. Each of the lists below answers a different
    question, and the dashboard shows several because no single ordering is
    the right one.
    """
    ds = list(dnas.values())
    keyfns = {
        "alpha": lambda d: -d.alpha_vs_band,
        "expectancy": lambda d: -d.expectancy,
        "risk_adjusted": lambda d: -d.sharpe_like,
        "consistency": lambda d: (d.max_drawdown, -d.profit_factor
                                  if d.profit_factor != float("inf") else -1e9),
        "win_rate": lambda d: -d.win_rate,
        "capital_efficiency": lambda d: -(d.roi / max(d.preferred_ttr_hours, 1)),
        "recent": lambda d: -d.last_ts,
        "diversity": lambda d: -d.markets,
    }
    return sorted(ds, key=keyfns.get(by, keyfns["alpha"]))


def cohorts(dnas: dict) -> dict:
    """The named lists the brief asks for.

    A wallet may appear in several. `HIGH_WIN_RATE` deliberately EXCLUDES
    wallets whose alpha is ~0, because a high win rate without alpha is the
    price band and putting it on a leaderboard is how the whole system learns
    the wrong lesson.
    """
    ds = [d for d in dnas.values() if d.evidence_quality != "INSUFFICIENT"]
    now = int(time.time())
    return {
        "TOP_WALLETS": [d.wallet for d in rank(dnas, by="alpha")[:20]
                        if d.alpha_vs_band > 0],
        "RISING_WALLETS": [d.wallet for d in ds
                           if d.last_ts > now - 7 * 86400
                           and d.alpha_vs_band > 0
                           and (d.last_ts - d.first_ts) < 30 * 86400][:20],
        "CONSISTENT_WALLETS": [d.wallet for d in ds
                               if d.max_drawdown < 0.2 and d.trades >= 150][:20],
        "HIGH_WIN_RATE_WALLETS": [d.wallet for d in ds
                                  if d.win_rate > 0.65
                                  and d.alpha_vs_band > 0.01][:20],
        "HIGH_EXPECTANCY_WALLETS": [d.wallet for d in
                                    rank(dnas, by="expectancy")[:20]
                                    if d.expectancy > 0],
        "REGIME_SPECIALISTS": [d.wallet for d in ds if d.burstiness > 2.0][:20],
        "EVENT_SPECIALISTS": [d.wallet for d in ds
                              if d.late_market_rate > 0.4][:20],
        "MICROSTRUCTURE_SPECIALISTS": [d.wallet for d in ds
                                       if d.repeat_market_rate > 0.5][:20],
    }


def copy_score(dna: WalletDNA, ev, ctx: dict) -> dict:
    """COPY_SCORE for one wallet event. 0..1, with the reason it is not higher.

    The objective is explicitly NOT "copy every trade". Each component can veto
    on its own — they are multiplied, not averaged — because a wallet with
    excellent history trading in a regime it has never traded is not a
    three-quarters-good idea.
    """
    blockers: list[str] = []
    comps: dict = {}

    comps["historical_edge"] = min(1.0, max(0.0, dna.alpha_vs_band / 0.05))
    if dna.alpha_vs_band <= 0:
        blockers.append(f"wallet alpha is {dna.alpha_vs_band:+.4f}; its record "
                        f"is explained by the price band it trades")

    q = {"STRONG": 1.0, "MODERATE": 0.7, "THIN": 0.35,
         "INSUFFICIENT": 0.0}.get(dna.evidence_quality, 0.0)
    comps["evidence_quality"] = q
    if q == 0.0:
        blockers.append(f"evidence quality {dna.evidence_quality}")

    price = float(ctx.get("market_probability") or 0.0)
    band = f"{_band(price)[0]:.2f}-{_band(price)[1]:.2f}"
    share = float(dna.price_band_mix.get(band, 0.0))
    comps["market_similarity"] = min(1.0, share * 3)
    if share < 0.05:
        blockers.append(f"wallet has traded price band {band} in only "
                        f"{share:.1%} of its history")

    if ev.regime.ok:
        conf = float(ev.regime.get("confidence") or 0.0)
        comps["regime_confidence"] = conf
        if conf < 0.4:
            blockers.append("regime classification is too weak to condition on")
    else:
        comps["regime_confidence"] = 0.0
        blockers.append("regime unknown")

    xw = float(ev.cross_wallet.get("convergence") or 0.0) if ev.cross_wallet.ok \
        else 0.0
    comps["cross_wallet_confirmation"] = xw

    news_ok = 1.0 if (ev.news.ok and abs(
        float(ev.news.get("weighted_direction") or 0)) > 0.05) else 0.5
    comps["news_confirmation"] = news_ok

    sz = ctx.get("sizing")
    comps["execution_feasibility"] = 1.0 if (sz is not None and sz.ok) else 0.0
    if sz is not None and not sz.ok:
        blockers.append(f"not executable: {sz.reason}")

    score = 1.0
    for v in comps.values():
        score *= max(v, 0.0)
    return {"score": round(score ** (1.0 / max(len(comps), 1)), 4)
            if not blockers else 0.0,
            "components": {k: round(v, 4) for k, v in comps.items()},
            "blockers": blockers}
