"""Shadow mode: run Strategy B over history through the FULL live pipeline.

The difference between this and a backtest, and it is the point of the module:

    a backtest asks   "would this rule have made money?"
    shadow mode asks  "would this SYSTEM have taken the trade, and if not,
                       which layer stopped it?"

So every signal goes through behaviour matching, Strategy B's conditions, the
status gate, sizing, the portfolio layer, the execution model and the account
-- the same code paths the live route uses, with simulated fills. That is what
makes the funnel numbers mean something: they are produced by the system that
would trade, not by a separate analysis of it.

It is also how the V1 deadlock is broken safely. Shadow mode moves no capital,
so it can run on any strategy status and accumulate the evidence that a live
ladder needs. V1 could not do this: its empirical gate required CLOSED
LIFECYCLES, and lifecycles only exist if you trade, so it could never bootstrap
and produced 40,820 identical DO_NOTHING decisions.

Positions are settled at the token's resolution, which is exact.
"""

from __future__ import annotations

import time

from .config import Settings
from .ledger import Funnel, LedgerStore, Mode
from .risk.compounding import new_account
from .strategy_b import rn1
from .strategy_b.behavior import BehaviorMatcher, CompositeMatcher
from .strategy_b.decompose import build_profile
from .strategy_b.engine import StrategyBEngine, StrategyBinding
from .strategy_b.strategy import CopyStrategy, naive_copy
from .substrate.data import PriceTape, oos_split_ts, wallet_trade_counts
from .substrate.state import collect, stream_observations
from .risk.sizing import ExpansionEvidence
from .validation.registry import Registry


def _bindings_from_registry(st: Settings, profiles: dict,
                            mode: str) -> list:
    """Bind whatever the registry has validated. Empty is a valid answer."""
    reg = Registry(st.work_dir / "research" / "registry.sqlite3")
    try:
        rows = reg.tradable()
    finally:
        reg.close()
    import json
    out = []
    for r in rows:
        wallet = r["wallet"]
        if wallet not in profiles:
            continue
        spec = json.loads(r["spec"])
        spec.pop("exit", None)
        strategy = CopyStrategy(**{k: v for k, v in spec.items()
                                   if k in CopyStrategy.__dataclass_fields__})
        oos = json.loads(r["oos"] or "{}")
        ev = ExpansionEvidence(
            sample_size=oos.get("n_filled", 0),
            oos_expectancy=oos.get("expectancy", 0.0),
            max_drawdown_pct=oos.get("max_drawdown_pct", 0.0),
            strategy_score=r["score"],
            size_predicts_win=profiles[wallet].sizing.size_predicts_win)
        out.append(StrategyBinding(strategy=strategy,
                                   matcher=BehaviorMatcher(profiles[wallet]),
                                   status=r["status"], evidence=ev, mode=mode))
    return out


def run_shadow(st: Settings, *, wallet: str | None = None,
               max_wallets: int = 8, mode: str = Mode.SHADOW.value,
               verbose: bool = False) -> dict:
    """Replay the out-of-sample window through the full Strategy B route."""
    t0 = time.time()
    st.ensure_dirs()
    split = oos_split_ts(st)

    # 1. the wallet universe, and profiles built IN-SAMPLE ONLY. Building a
    #    profile on the window we are about to replay would be look-ahead
    #    dressed as a shadow run.
    if wallet:
        wallets = [wallet]
    else:
        ref = rn1.select_reference(st, None)
        counts = [w for w, _ in wallet_trade_counts(
            st, st.strategy_b.min_wallet_trades)][:max_wallets]
        wallets = list(dict.fromkeys([ref.wallet] + counts))[:max_wallets]

    profiles = {}
    for w in wallets:
        is_obs = collect(st, wallets=[w], ts_to=split)
        if len(is_obs) >= 20:
            profiles[w] = build_profile(w, is_obs)
    if verbose:
        print(f"profiled {len(profiles)} wallets in {time.time() - t0:.0f}s")

    # 2. bindings. Validated strategies if any exist; otherwise the naive-copy
    #    baseline in SHADOW so the pipeline is exercised and the funnel is
    #    measurable. Shadow moves no capital, so this is safe -- and it is the
    #    ONLY way to obtain the operating evidence a live ladder later needs.
    bindings = _bindings_from_registry(st, profiles, mode)
    bootstrapped = False
    if not bindings:
        bootstrapped = True
        for w, prof in profiles.items():
            bindings.append(StrategyBinding(
                strategy=naive_copy(w, delay_secs=st.strategy_b.delay_secs),
                matcher=BehaviorMatcher(prof), status="UNVALIDATED",
                evidence=ExpansionEvidence(
                    size_predicts_win=prof.sizing.size_predicts_win),
                mode=Mode.SHADOW.value))

    # 3. drive the pipeline
    account = new_account(st)
    tape = PriceTape(st)
    ledger = LedgerStore(st.work_dir / "research" / "ledger.sqlite3")
    engine = StrategyBEngine(st, account, bindings=bindings, tape=tape,
                             mode=mode, ledger=ledger, funnel=Funnel())

    open_keys: list = []
    n_obs = 0
    for o in stream_observations(st, wallets=list(profiles), ts_from=split):
        n_obs += 1
        for rec in engine.evaluate(o):
            if rec.stage == "EXECUTION_SUCCESSFUL":
                open_keys.append((f"{o.trade.token_id}:{rec.signal_id}",
                                  o.trade, rec.fill_price))
        # 4. settle anything whose resolution the clock has now passed. Exact,
        #    not modelled: the token resolved to 0 or 1.
        still: list = []
        for key, tr, entry in open_keys:
            settle_at = tr.settled_ts or 0
            if settle_at and settle_at <= o.trade.ts:
                ret = (tr.resolution - entry) / entry if entry > 0 else 0.0
                engine.settle(key, ret, o.trade.ts)
            else:
                still.append((key, tr, entry))
        open_keys = still
        if verbose and n_obs % 2000 == 0:
            print(f"  {n_obs:,} observations, "
                  f"{len(account.positions)} open, "
                  f"equity {account.equity:,.0f}", flush=True)

    # 5. settle the remainder at their resolutions
    for key, tr, entry in open_keys:
        ret = (tr.resolution - entry) / entry if entry > 0 else 0.0
        engine.settle(key, ret, tr.settled_ts or tr.ts)

    engine.flush()
    ledger.close()
    report = engine.report()
    report.update({
        "observations": n_obs,
        "wallets": list(profiles),
        "bindings_bootstrapped": bootstrapped,
        "seconds": round(time.time() - t0, 1),
        "note": (
            "Bindings are naive-copy baselines in SHADOW because the registry "
            "holds no VALIDATED strategy. No capital is at risk and no "
            "strategy is promoted by this run; its purpose is to exercise the "
            "pipeline and produce the funnel."
            if bootstrapped else
            f"{len(bindings)} VALIDATED strategies bound."),
    })
    return report
