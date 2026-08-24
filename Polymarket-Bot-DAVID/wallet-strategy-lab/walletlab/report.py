"""Human-readable output (S37). Computes nothing -- every number is passed in."""

from __future__ import annotations

from .backtest import run
from .strategy import naive_copy


def render_inventory(inv: dict, grid: int) -> str:
    L = []
    A = L.append
    A("SUBSTRATE INVENTORY")
    A("=" * 72)
    A(f"  tape span                     {inv['tape_days']} days")
    A(f"  wallet trades (all types)     {inv['wallet_trades_total']:,}")
    A(f"  distinct wallets              {inv['wallets_total']:,}")
    A(f"  distinct markets              {inv['markets_total']:,}")
    A(f"  settled resolutions           {inv['resolutions']:,}")
    A("")
    A("EVALUABLE UNIVERSE  (BUY, settled 0/1, inside the price band)")
    A(f"  copyable trades               {inv['settled_copyable_trades']:,}")
    A(f"  distinct wallets              {inv['settled_wallets']:,}")
    A(f"  distinct settled tokens       {inv['settled_tokens']:,}")
    A("")
    A("WALLETS WITH ENOUGH EVIDENCE")
    for k in (50, 100, 200, 500):
        A(f"  >= {k:>3} settled trades         {inv[f'wallets_ge_{k}_settled']:,}")
    A("")
    A("MULTIPLE-TESTING BUDGET (S34)")
    n100 = inv["wallets_ge_100_settled"]
    A(f"  transformation grid            {grid:,} per wallet")
    A(f"  eligible wallets (>=100)       {n100:,}")
    A(f"  hypotheses in a full sweep     {grid * n100:,}")
    A(f"  false 'winners' at p<0.05      ~{int(grid * n100 * 0.05):,}")
    A("")
    A("  This is why the engine gates on Benjamini-Hochberg over the whole pass")
    A("  rather than on a per-strategy p-value. A sweep this wide will always")
    A("  produce spectacular backtests; the denominator is what makes them")
    A("  interpretable.")
    return "\n".join(L)


def render_wallet(wallet: str, obs, st, tape) -> str:
    L = []
    A = L.append
    base = run(naive_copy(wallet), obs, st, tape)
    A(f"WALLET {wallet}")
    A("=" * 72)
    A(f"  settled copyable trades       {len(obs):,}")
    A(f"  distinct markets              {len(base.markets):,}")
    if obs:
        A(f"  first / last trade            {obs[0].trade.ts} .. {obs[-1].trade.ts}")
    A("")
    A("NAIVE COPY BASELINE (S46), after modelled costs")
    for k, v in base.summary().items():
        A(f"  {k:28s} {v}")
    A("")
    A("PRICE-BAND CONDITIONALITY (S32) -- where does the edge actually live?")
    A(f"  {'band':>14} {'n':>6} {'expectancy':>12} {'win rate':>10}")
    for lo, hi in ((0.02, 0.20), (0.20, 0.40), (0.40, 0.60), (0.60, 0.80), (0.80, 0.98)):
        sel = [o for o in obs if lo <= o.price < hi]
        if not sel:
            continue
        rets = [(o.trade.resolution - st.costs.fill_price(o.price)) /
                st.costs.fill_price(o.price) for o in sel]
        wr = sum(1 for o in sel if o.trade.resolution > 0.5) / len(sel)
        A(f"  {lo:.2f}-{hi:.2f}    {len(sel):6d} {sum(rets)/len(rets):+12.4f} {wr:10.3f}")
    A("")
    A("  A band that looks strong here is a HYPOTHESIS, not a finding -- it was")
    A("  chosen after seeing the data. discover-strategies is what tests it")
    A("  out-of-sample with the multiple-testing denominator attached.")
    return "\n".join(L)


def render_discovery(rep: dict) -> str:
    if "error" in rep:
        return f"ERROR: {rep['error']}"
    L = []
    A = L.append
    A("DISCOVERY PASS")
    A("=" * 72)
    A(f"  wallets analysed              {rep['wallets_analysed']}")
    A(f"  observations                  {rep['observations']:,}")
    A(f"  hypotheses tested             {rep['hypotheses_tested']:,}")
    A(f"  reached out-of-sample         {rep['reached_oos']:,}")
    A(f"  BH threshold (FDR={rep['fdr']})       p <= {rep['bh_threshold']:.6f}")
    A(f"  VALIDATED                     {rep['validated']}")
    A("")
    if rep.get("status_counts"):
        A("OUTCOME OF EVERY CANDIDATE THAT REACHED OUT-OF-SAMPLE")
        for k, v in sorted(rep["status_counts"].items(), key=lambda x: -x[1]):
            A(f"  {k:26s} {v}")
        A("")
    if not rep["top"]:
        A("NO EDGE DETECTED.")
        A("")
        A("  No candidate survived out-of-sample testing at the false-discovery")
        A("  rate this pass demands. Given the hypothesis count above, that is")
        A("  the statistically correct outcome -- not a failure of the engine")
        A("  and not a reason to lower the bar (S38, S39).")
        return "\n".join(L)

    A("TOP VALIDATED STRATEGIES (S35, sorted by robustness -- not by P&L)")
    for i, t in enumerate(rep["top"], 1):
        A("")
        A(f"  [{i}] {t['wallet']}  ({t['spec_hash']})")
        A(f"      score {t['score']}   oos_p {t['oos_p']:.5f}")
        cond = {k: v for k, v in t["spec"].items()
                if v not in (None, False, 0, "flat", 100.0, 0.05, "")}
        A(f"      conditions: {cond or 'unconditional (naive copy)'}")
        tt = t["test"]
        A(f"      oos: n={tt['n_filled']} expectancy={tt['expectancy']:+.4f} "
          f"markets={tt['n_markets']} conc={tt['concentration']:.2f}")
        pop = t.get("population") or {}
        A(f"      WALLET ALPHA {t.get('alpha', 0):+.4f}   "
          f"(population same band/window: {pop.get('expectancy', 'n/a')} on n={pop.get('n', 0)})")
        A(f"      walk-forward consistency {t['walk_consistency']:.2f}   "
          f"cross-wallet {t['cross_wallet_consistency']:.2f}")
        if t["robustness"]:
            A(f"      robustness: {t['robustness']}")
    return "\n".join(L)
