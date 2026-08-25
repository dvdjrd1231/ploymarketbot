"""The V2 command line.

Every command is read-only with respect to the original installation. The only
paths V2 writes are under `work_dir` (default `polymarket_quant_v2/var/`).

    python -m pqv2 inventory            measure the substrate
    python -m pqv2 audit                where V1's opportunities go, and why
    python -m pqv2 gates                who owns which suppression
    python -m pqv2 rn1                  reconstruct the reference wallet
    python -m pqv2 discover             the full discovery + validation pass
    python -m pqv2 leaderboard          what survived
    python -m pqv2 features             feature information + inert axes
    python -m pqv2 exits                settlement vs early exit
    python -m pqv2 expansion            the Win Expansion ladder
    python -m pqv2 shadow               run Strategy B over history, ledgered
    python -m pqv2 dashboard            everything, on one screen
    python -m pqv2 diagnose             the brief's 22 questions
    python -m pqv2 accel                acceleration status / build trigger
    python -m pqv2 selftest             fast end-to-end check
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import Settings


def _p(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _settings(args) -> Settings:
    st = Settings()
    if getattr(args, "data_db", None):
        st.data_db = Path(args.data_db)
    if getattr(args, "work_dir", None):
        st.work_dir = Path(args.work_dir)
    if getattr(args, "capital", None):
        st.compounding.starting_capital = float(args.capital)
    if getattr(args, "oos", None):
        st.oos_fraction = float(args.oos)
    return st.ensure_dirs()


def _load_report(st: Settings) -> dict:
    p = st.work_dir / "reports" / "last_pass.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _save_report(st: Settings, name: str, payload) -> Path:
    p = st.work_dir / "reports" / name
    p.write_text(json.dumps(payload, indent=2, default=str))
    return p


# --- commands --------------------------------------------------------------

def cmd_inventory(args) -> int:
    from .substrate.data import inventory
    st = _settings(args)
    inv = inventory(st)
    _p(inv)
    print(f"\nThe evaluable substrate is {inv['settled_copyable_trades']:,} "
          f"trades over {inv['settled_markets']:,} markets and "
          f"{inv['settled_days']} days.")
    if inv["resolutions_missing_settled_ts"]:
        print(f"WARNING: settled_ts is 0 in "
              f"{inv['resolutions_missing_settled_ts']:,} of "
              f"{inv['resolutions']:,} resolutions. Point-in-time wallet track "
              "record falls back to observation time - safe, but blunt. This "
              "is the highest-value backfill in the project.")
    return 0


def cmd_audit(args) -> int:
    from .strategy_a.adapter import inspect, preserve_verdict, inherited_gates
    st = _settings(args)
    state = inspect(st)
    print("STRATEGY A - OPPORTUNITY AUDIT")
    print("=" * 70)
    print(f"decisions   {state.decisions_total:,}")
    print(f"executions  {state.executions:,}")
    print(f"lifecycles  {state.lifecycles:,}")
    print(f"\nactions: {state.actions}")
    print("\ntop reasons:")
    for reason, n in state.reasons[:5]:
        print(f"  {n:>8,}  {reason[:100]}")
    print(f"\nlibrary: {state.library_statuses}")
    print(f"tradable strategies: {state.tradable_strategies}")
    print(f"\nBLOCKING GATE: {state.blocking_gate}")
    print(f"\nVERDICT\n{state.verdict}")
    print(f"\nRECOMMENDATION\n{state.recommendation}")
    print("\nPRESERVATION")
    _p(preserve_verdict(state))
    print("\nGATES V2 DECLINED TO INHERIT")
    for g in inherited_gates():
        print(f"  {g['gate']}: {g['what']}")

    from .strategy_a.adapter import orphaned_evidence
    orphan = orphaned_evidence(st)
    if orphan.get("validated"):
        print("\n" + "!" * 70)
        print("ORPHANED EVIDENCE: VALIDATED STRATEGIES THAT NOTHING READS")
        print("!" * 70)
        print(f"  source: {orphan['path']}")
        print(f"  statuses: {orphan['status_histogram']}")
        for v in orphan["validated"]:
            print(f"\n  wallet {v['wallet'][:14]}  score {v['score']}  "
                  f"oos_p {v['oos_p']:.2e}")
            print(f"    price band {v['price_band']}  delay {v['delay_secs']}s")
            print(f"    test: expectancy {v['test_expectancy']:+.4f} over "
                  f"{v['test_fills']} fills / {v['test_markets']} markets, "
                  f"win rate {v['test_win_rate']}")
        print("\n  CAVEATS - read before acting on any of this:")
        for c in orphan["caveats"]:
            for line in _wrap_text(c, 66):
                print(f"    {line}")
    _save_report(st, "strategy_a_audit.json",
                 {"strategy_a": state.to_dict(), "orphaned_evidence": orphan})
    return 0


def _wrap_text(text: str, width: int) -> list:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def cmd_gates(args) -> int:
    from . import gates
    st = _settings(args)
    a = gates.audit()
    print("GATE OWNERSHIP")
    print("=" * 70)
    for owner, keys in sorted(a["by_owner"].items()):
        print(f"\n{owner} ({len(keys)})")
        for k in keys:
            g = gates.REGISTRY[k]
            print(f"  {k:<32} {g.description}")
    if a["unjustified_global"]:
        print(f"\n!! GLOBAL_SAFETY gates with no written evidence: "
              f"{a['unjustified_global']}")
        print("   A global gate with no evidence is a Strategy A gate in "
              "disguise. Fix or reclassify it.")
    else:
        print("\nEvery GLOBAL_SAFETY gate carries written evidence.")
    _save_report(st, "gate_audit.json", a)
    return 0


def cmd_rn1(args) -> int:
    from .strategy_b import rn1
    st = _settings(args)
    if args.candidates:
        _p(rn1.identify_candidates(st, top=args.top))
        return 0
    ref = rn1.select_reference(st, args.wallet)
    rec = rn1.reconstruct(st, ref)
    print(f"REFERENCE WALLET: {ref.wallet}")
    print(f"provenance: {ref.provenance}")
    if ref.costs_power:
        print(f"selection pool: {ref.selection_pool} wallets  "
              f"(counted in the multiple-testing budget)")
    print(f"\n{ref.note}\n")
    p = rec.profile
    print(f"observations (in-sample): {p.n_observations:,}")
    print(f"entry price   p10/p50/p90  {p.entry.price['p10']:.3f} / "
          f"{p.entry.price['p50']:.3f} / {p.entry.price['p90']:.3f}")
    print(f"stake         median ${p.sizing.median_notional:,.0f}  "
          f"conviction ratio {p.sizing.conviction_ratio:.2f}  "
          f"dispersion {p.sizing.dispersion:.2f}")
    print(f"size predicts wins: {p.sizing.size_predicts_win:+.3f}  "
          "(big-vs-small win-rate gap; ~0 means size carries no information)")
    print(f"activity      {p.risk.trades_per_day:.1f} trades/day   "
          f"market HHI {p.risk.market_concentration:.3f}")
    print(f"opening entries {p.entry.opening_entry_share:.0%}   "
          f"new markets {p.entry.new_market_share:.0%}")
    print(f"outcome (hold to resolution, gross): n={p.outcome.n:,}  "
          f"win {p.outcome.win_rate:.1%}  expectancy "
          f"{p.outcome.expectancy:+.4f}  PF {p.outcome.profit_factor:.2f}")
    print(f"              avg win {p.outcome.avg_win:+.3f}  "
          f"avg loss {p.outcome.avg_loss:+.3f}  "
          f"largest loss {p.outcome.largest_loss:+.3f}")
    if rec.families:
        print("\nBEHAVIOURAL FAMILIES INSIDE THIS WALLET (in-sample; each must "
              "be validated separately):")
        for f in rec.families[:6]:
            print(f"  {f['family']:<34} n={f['n']:<5} "
                  f"exp={f['expectancy']:+.4f} "
                  f"delta={f['delta_vs_wallet']:+.4f}")
    if p.notes:
        print("\nNOTES")
        for n in p.notes:
            print(f"  - {n}")
    print("\nWHAT THIS RECONSTRUCTION CANNOT ANSWER")
    for lim in rec.limits:
        print(f"  - {lim}")
    _save_report(st, "rn1.json", rec.to_dict())
    return 0


def cmd_discover(args) -> int:
    from .strategy_b import discover
    st = _settings(args)
    t0 = time.time()
    rep = discover.run_pass(
        st, wallet=args.wallet, max_wallets=args.max_wallets,
        deep=not args.fast,
        progress=(lambda m: print(f"  [{time.time() - t0:6.0f}s] {m}",
                                  flush=True)) if args.verbose else None)
    _save_report(st, "last_pass.json", rep.to_dict())
    print(f"\nwallets {len(rep.wallets)}   hypotheses "
          f"{rep.hypotheses_tested:,}   BH p<={rep.bh_threshold:.5f}   "
          f"{rep.seconds}s")
    print("\nstatus histogram:")
    for s, n in rep.status_histogram:
        print(f"  {s:<24}{n:>8,}")
    print(f"\nvalidated: {len(rep.validated)}")
    for v in rep.validated[:10]:
        print(f"  {v['score']:.3f}  {v['wallet'][:12]}  "
              f"exp {v['oos'].get('expectancy', 0):+.4f}  "
              f"alpha {v['alpha'].get('alpha', 0):+.4f}  "
              f"n={v['oos'].get('n_filled', 0)}  {v['describe'][:50]}")
    if rep.agreement:
        print("\ncross-wallet agreement (the only evidence not vulnerable to "
              "wallet selection):")
        for r in rep.agreement[:6]:
            print(f"  validated on {r['wallets_validated']}, positive on "
                  f"{r['wallets_positive']}/{r['wallets_tested']}  "
                  f"alpha {r['mean_alpha']:+.4f}  {r['describe'][:48]}")
    for n in rep.notes:
        print(f"\n- {n}")
    return 0


def cmd_leaderboard(args) -> int:
    from .validation.registry import Registry
    st = _settings(args)
    reg = Registry(st.work_dir / "research" / "registry.sqlite3")
    rows = reg.leaderboard(status=args.status, limit=args.top)
    print(f"{'score':>6} {'status':<22}{'wallet':<14}{'exp':>9}{'alpha':>9}"
          f"{'n':>7}{'mkts':>6}  describe")
    for r in rows:
        oos = json.loads(r["oos"] or "{}")
        alpha = json.loads(r["alpha"] or "{}")
        print(f"{r['score']:>6.3f} {r['status']:<22}{r['wallet'][:12]:<14}"
              f"{oos.get('expectancy', 0):>+9.4f}{alpha.get('alpha', 0):>+9.4f}"
              f"{oos.get('n_filled', 0):>7,}{oos.get('n_markets', 0):>6}  "
              f"{(r['describe'] or '')[:44]}")
    print(f"\nhistogram: {reg.status_histogram()}")
    reg.close()
    return 0


def cmd_features(args) -> int:
    from .research.features import audit_features
    from .substrate.data import oos_split_ts
    from .substrate.state import collect
    st = _settings(args)
    obs = collect(st, ts_to=oos_split_ts(st), limit=args.limit)
    print(f"auditing {len(obs):,} observations\n")
    a = audit_features(obs)
    print(f"{'feature':<26}{'distinct':>9}{'lift':>11}{'t':>8}{'p':>10}  note")
    for f in a["features"]:
        note = "INERT" if f["inert"] else ""
        print(f"{f['name']:<26}{f['distinct']:>9,}{f['lift']:>+11.4f}"
              f"{f['t_stat']:>8.2f}{f['p_value']:>10.5f}  {note}")
    print(f"\n{a['note']}")
    _save_report(st, "feature_audit.json", a)
    return 0


def cmd_exits(args) -> int:
    from .research.exits import compare
    from .strategy_b.strategy import naive_copy
    from .substrate.data import PriceTape, oos_split_ts
    from .substrate.state import collect
    st = _settings(args)
    from .strategy_b import rn1
    ref = rn1.select_reference(st, args.wallet)
    obs = collect(st, wallets=[ref.wallet], ts_from=oos_split_ts(st))
    tape = PriceTape(st)
    c = compare(naive_copy(ref.wallet), obs, st, tape)
    print(f"EXIT COMPARISON - {ref.wallet[:16]}  ({len(obs)} observations)\n")
    print(f"{'exit':<34}{'n':>6}{'exp':>10}{'win':>8}{'PF':>7}{'W/L':>7}"
          f"{'tail':>9}  confidence")
    for r in c.rows:
        print(f"{r['exit']:<34}{r['n_filled']:>6}{r['expectancy']:>+10.4f}"
              f"{r['win_rate']:>8.1%}{r['profit_factor']:>7.2f}"
              f"{r['win_loss_ratio']:>7.2f}{r['tail_loss_p05']:>+9.4f}  "
              f"{r['confidence']}")
    print(f"\nVERDICT: {c.verdict}")
    _save_report(st, "exits.json", c.to_dict())
    return 0


def cmd_reconcile(args) -> int:
    """Reconciliation exit-safety: before/after and the demo replay."""
    from .report import reconciliation_report as rr
    st = _settings(args)
    out = rr.build(st)
    print(out["rendered"])

    if args.demo:
        from .reconciliation import PositionEvidence, ReconciliationGuard
        print("\nDEMONSTRATION - the observed failure pattern, replayed")
        print("-" * 62)
        # 20 held positions, one snapshot that returns nothing (an API blip).
        events = [PositionEvidence(
            token_id=f"tok{i}", local_size=100.0, local_entry_ts=0.0,
            last_confirmed_ts=1000.0, snapshot_ts=2000.0,
            snapshot_present=False, snapshot_total_positions=0)
            for i in range(20)]
        res = rr.replay(events, guard=ReconciliationGuard())
        print(f"  a single empty snapshot arrives while 20 positions are held")
        print(f"  old path would close : {res['would_have_exited_before']:>3}")
        print(f"  patched path closes  : {res['exits_confirmed']:>3}")
        print(f"  prevented            : {res['exits_prevented']:>3}")
        _save_report(st, "reconciliation_demo.json", res)
    _save_report(st, "reconciliation.json",
                 {k: v for k, v in out.items() if k != "rendered"})
    return 0


def cmd_winners(args) -> int:
    """STEP 28: what distinguishes the tails."""
    from .research.winners import decompose, separating_features
    from .strategy_b.strategy import naive_copy
    from .substrate.data import PriceTape, oos_split_ts
    from .substrate.state import collect
    from .validation import backtest
    from .strategy_b import rn1
    st = _settings(args)
    ref = rn1.select_reference(st, args.wallet)
    obs = collect(st, wallets=[ref.wallet], ts_from=oos_split_ts(st))
    res = backtest.run(naive_copy(ref.wallet), obs, st, PriceTape(st))
    if not res.fills:
        print("no fills to decompose")
        return 0
    out = decompose(res.fills)
    print(f"WINNER / LOSER DECOMPOSITION - {ref.wallet[:16]}  "
          f"({res.n_filled} fills)\n")
    print(f"{'bucket':<16}{'n':>6}{'share':>8}{'mean ret':>11}"
          f"{'pnl':>11}{'pnl share':>11}")
    for b in out["buckets"]:
        print(f"{b['bucket']:<16}{b['n']:>6}{b['share']:>8.1%}"
              f"{b['mean_return']:>+11.4f}{b['total_pnl']:>11,.0f}"
              f"{b['pnl_share']:>+11.1%}")
    a = out["asymmetry"]
    print(f"\nwin rate {a['win_rate']:.1%}   expectancy {a['expectancy']:+.4f}"
          f"   profit factor {a['profit_factor']:.2f}")
    print(f"avg win {a['avg_win']:+.4f} / avg loss {a['avg_loss']:+.4f}"
          f"   W/L ratio {a['win_loss_ratio']:.2f}")
    print(f"largest win {a['largest_win']:+.3f} / largest loss "
          f"{a['largest_loss']:+.3f}")
    print(f"top 5% of trades carry {a['tail_dependence_top5pct']:.0%} of profit")
    print(f"\nINTERPRETATION: {out['note']}")
    print("\nENTRY-TIME FEATURES SEPARATING LARGE WINNERS FROM LARGE LOSERS")
    print("(uncorrected p-values - research pointers, not findings)")
    for row in separating_features(res.fills):
        if "note" in row:
            print(f"  {row['note']}")
        else:
            print(f"  {row['feature']:<18} winners {row['winner_mean']:>12,.3f}"
                  f"  losers {row['loser_mean']:>12,.3f}"
                  f"  t {row['t_stat']:>6.2f}  p {row['p_value_uncorrected']:.4f}")
    _save_report(st, "winners.json", out)
    return 0


def cmd_expansion(args) -> int:
    from .risk.sizing import fit_expansion
    from .risk.compounding import compare_sizing_modes
    from .strategy_b.strategy import naive_copy
    from .substrate.data import PriceTape, oos_split_ts
    from .substrate.state import collect
    from .validation import backtest
    from .strategy_b import rn1
    st = _settings(args)
    ref = rn1.select_reference(st, args.wallet)
    obs = collect(st, wallets=[ref.wallet], ts_from=oos_split_ts(st))
    res = backtest.run(naive_copy(ref.wallet), obs, st, PriceTape(st))
    fit = fit_expansion(res.fills)
    print("WIN EXPANSION LADDER")
    print(f"{'mult':>6}{'pnl':>12}{'roi':>10}{'max dd':>11}{'pnl/dd':>9}"
          f"{'tail p05':>11}")
    for r in fit["rows"]:
        print(f"{r['multiplier']:>6.2f}{r['pnl']:>12,.0f}{r['roi']:>+10.4f}"
              f"{r['max_drawdown']:>11,.0f}"
              f"{r['pnl'] / (r['max_drawdown'] or 1):>9.2f}"
              f"{r['tail_loss_p05']:>+11.4f}")
    print(f"\nrecommended {fit['recommended']:.2f}x - {fit['note']}")
    print("\nSTAKING MODES (same trades, same order; only the rule differs)")
    print(f"{'mode':<20}{'equity':>12}{'return':>10}{'max dd':>10}{'PF':>7}")
    for m in compare_sizing_modes(st, res.fills):
        print(f"{m['mode']:<20}{m['equity']:>12,.0f}"
              f"{m['compounded_return']:>+10.1%}{m['max_drawdown']:>10.1%}"
              f"{m['profit_factor']:>7.2f}")
    _save_report(st, "expansion.json", fit)
    return 0


def cmd_shadow(args) -> int:
    from .shadow import run_shadow
    st = _settings(args)
    out = run_shadow(st, wallet=args.wallet, max_wallets=args.max_wallets,
                     verbose=args.verbose)
    _save_report(st, "shadow.json", out)
    print(json.dumps(out["funnel"], indent=2, default=str))
    print(json.dumps(out["account"], indent=2, default=str))
    return 0


def cmd_gui(args) -> int:
    """Render the visual dashboard as a self-contained HTML file."""
    import time as _time
    import webbrowser
    from .report.html_dashboard import write
    st = _settings(args)
    reports = st.work_dir / "reports"
    if not reports.exists() or not any(reports.glob("*.json")):
        print("No reports yet. Run this first:\n"
              "  python -m pqv2 discover -v\n"
              "  python -m pqv2 shadow\n"
              "or just double-click INSTALL.bat, which does everything.")
        return 1

    # The favourite-longshot table is a static property of the dataset, and
    # measuring it is a full scan. Cache it, so double-clicking the launcher
    # is instant after the first run rather than ten seconds of silence.
    calibration = None
    cache = reports / "calibration.json"
    if not args.no_calibration:
        if cache.exists() and not args.refresh:
            try:
                calibration = json.loads(cache.read_text())
            except ValueError:
                calibration = None
        if calibration is None:
            try:
                from .validation.baseline import calibration_table
                print("measuring the favourite-longshot bias (one DB scan)...")
                calibration = calibration_table(st)
                cache.write_text(json.dumps(calibration, indent=2))
            except Exception as exc:                          # noqa: BLE001
                print(f"  (skipped: {exc})")

    out = write(reports, st.work_dir / "dashboard.html",
                calibration=calibration,
                generated=_time.strftime("generated %Y-%m-%d %H:%M"))
    print(f"\nwrote {out}")
    if not args.no_open:
        webbrowser.open(out.resolve().as_uri())
        print("opened in your browser")
    return 0


def cmd_dashboard(args) -> int:
    from .report.dashboard import render
    from .strategy_a.adapter import inspect
    from .validation.registry import Registry
    from . import gates
    from .accel import Accelerator
    st = _settings(args)
    pr = _load_report(st)
    shadow = {}
    sp = st.work_dir / "reports" / "shadow.json"
    if sp.exists():
        shadow = json.loads(sp.read_text())
    reg = Registry(st.work_dir / "research" / "registry.sqlite3")
    fa = {}
    fp = st.work_dir / "reports" / "feature_audit.json"
    if fp.exists():
        fa = json.loads(fp.read_text())
    ex = {}
    xp = st.work_dir / "reports" / "expansion.json"
    if xp.exists():
        ex = json.loads(xp.read_text())
    print(render(pass_report=pr, funnel=shadow.get("funnel"),
                 account=shadow.get("account"),
                 strategy_a=inspect(st).to_dict(), gate_audit=gates.audit(),
                 expansion=ex, feature_audit=fa,
                 accel=Accelerator(st).status(),
                 leaderboard=reg.leaderboard(limit=15)))
    reg.close()
    return 0


def cmd_diagnose(args) -> int:
    from .report.diagnostics import build
    from .strategy_a.adapter import inspect
    from .research.ai import Researcher
    from . import gates
    st = _settings(args)
    pr = _load_report(st)
    shadow = {}
    sp = st.work_dir / "reports" / "shadow.json"
    if sp.exists():
        shadow = json.loads(sp.read_text())
    fa = {}
    fp = st.work_dir / "reports" / "feature_audit.json"
    if fp.exists():
        fa = json.loads(fp.read_text())
    sa = inspect(st).to_dict()
    hyp = [h.to_dict() for h in Researcher().propose(
        pass_report=pr, feature_audit=fa or None, strategy_a=sa)]
    d = build(pass_report=pr, funnel=shadow.get("funnel"),
              account=shadow.get("account"), strategy_a=sa,
              gate_audit=gates.audit(), hypotheses=hyp, feature_audit=fa or None)
    print(d.render())
    _save_report(st, "diagnostic.json", d.to_dict())
    return 0


def cmd_accel(args) -> int:
    from .accel import Accelerator, should_build
    from .substrate.data import inventory
    from .strategy_b.strategy import grid_size
    st = _settings(args)
    acc = Accelerator(st)
    _p(acc.status())
    pr = _load_report(st)
    inv = inventory(st) if args.measure else {}
    print("\nBUILD TRIGGER")
    _p(should_build(tape_rows=inv.get("wallet_trades_total", 878_650),
                    hypotheses_per_wallet=grid_size(),
                    pass_seconds=pr.get("seconds", 0.0)))
    return 0


def cmd_selftest(args) -> int:
    """Fast end-to-end check against the real database."""
    from .substrate.data import inventory
    from . import gates
    st = _settings(args)
    checks = []

    inv = inventory(st)
    checks.append(("substrate reachable", inv["settled_copyable_trades"] > 0,
                   f"{inv['settled_copyable_trades']:,} copyable trades"))
    a = gates.audit()
    checks.append(("every global gate carries evidence",
                   not a["unjustified_global"],
                   str(a["unjustified_global"])))

    from .ledger import Funnel, SignalRecord, Stage
    rec = SignalRecord(signal_id="T1", ts=0, route="B")
    try:
        rec.reject(Stage.STRATEGY_REJECTED, "v1.learning_mode", "should raise")
        ok = False
    except AssertionError:
        ok = True
    checks.append(("Strategy A gates cannot block Strategy B", ok,
                   "gates.assert_may_block raised as required"))

    f = Funnel()
    f.record(SignalRecord(signal_id="T2", ts=0, route="B"))
    try:
        f.assert_balanced()
        ok = True
    except AssertionError as exc:
        ok = False
    checks.append(("funnel reconciles", ok, ""))

    print("SELFTEST")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<48} {detail}")
    return 0 if all(c[1] for c in checks) else 1


# --- wiring ----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pqv2", description="Polymarket Quant Engine V2")
    p.add_argument("--data-db", help="read-only path to intel.sqlite3")
    p.add_argument("--work-dir", help="where V2 writes (never the V1 install)")
    p.add_argument("--capital", type=float, help="starting capital")
    p.add_argument("--oos", type=float, help="out-of-sample time fraction")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("inventory").set_defaults(fn=cmd_inventory)
    sub.add_parser("audit").set_defaults(fn=cmd_audit)
    sub.add_parser("gates").set_defaults(fn=cmd_gates)

    s = sub.add_parser("rn1")
    s.add_argument("--wallet")
    s.add_argument("--candidates", action="store_true")
    s.add_argument("--top", type=int, default=10)
    s.set_defaults(fn=cmd_rn1)

    s = sub.add_parser("discover")
    s.add_argument("--wallet")
    s.add_argument("--max-wallets", type=int)
    s.add_argument("--fast", action="store_true",
                   help="skip walk-forward/bootstrap/placebo (triage only)")
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("leaderboard")
    s.add_argument("--status")
    s.add_argument("--top", type=int, default=25)
    s.set_defaults(fn=cmd_leaderboard)

    s = sub.add_parser("features")
    s.add_argument("--limit", type=int, default=40000)
    s.set_defaults(fn=cmd_features)

    s = sub.add_parser("exits")
    s.add_argument("--wallet")
    s.set_defaults(fn=cmd_exits)

    s = sub.add_parser("reconcile")
    s.add_argument("--demo", action="store_true",
                   help="replay the observed failure pattern")
    s.set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("winners")
    s.add_argument("--wallet")
    s.set_defaults(fn=cmd_winners)

    s = sub.add_parser("expansion")
    s.add_argument("--wallet")
    s.set_defaults(fn=cmd_expansion)

    s = sub.add_parser("shadow")
    s.add_argument("--wallet")
    s.add_argument("--max-wallets", type=int, default=8)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_shadow)

    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("gui", help="visual dashboard as an HTML page")
    s.add_argument("--no-open", action="store_true")
    s.add_argument("--no-calibration", action="store_true",
                   help="skip the favourite-longshot scan (faster)")
    s.add_argument("--refresh", action="store_true",
                   help="re-measure the favourite-longshot table")
    s.set_defaults(fn=cmd_gui)
    sub.add_parser("diagnose").set_defaults(fn=cmd_diagnose)

    s = sub.add_parser("accel")
    s.add_argument("--measure", action="store_true")
    s.set_defaults(fn=cmd_accel)

    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
