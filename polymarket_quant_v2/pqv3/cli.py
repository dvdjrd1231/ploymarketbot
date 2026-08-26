"""V3 command line.

Every command prints what it measured, not what it hoped. Where a number cannot
be computed the output says so rather than printing 0 — a zero in a report is a
measurement, and printing one you did not take is the cheapest way to mislead
the person reading it.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .config import Mode, Settings, load
from .runtime import Engine


def _hr(title: str) -> None:
    print(f"\n{title}\n" + "-" * max(len(title), 40))


def _kv(k: str, v, width: int = 34) -> None:
    print(f"  {k:<{width}} {v}")


def _p(v) -> str:
    from .research.stats import format_p
    return format_p(float(v))


def _fmt(v, nd: int = 4) -> str:
    if v is None:
        return "— (not measured)"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


# ---------------------------------------------------------------- commands
def cmd_startup(args, st: Settings) -> int:
    eng = Engine(st)
    steps = eng.start(build_dna=not args.no_dna, max_wallets=args.max_wallets)
    _hr("STARTUP SEQUENCE")
    for s in steps:
        mark = "ok " if s.ok else "FAIL"
        print(f"  {s.n:>2}. [{mark}] {s.name:<34} {s.elapsed_ms:>6}ms")
        if s.detail:
            for line in str(s.detail).split(" | "):
                print(f"        {line}")
    bad = [s for s in steps if not s.ok]
    print(f"\n  {len(steps) - len(bad)}/{len(steps)} steps succeeded")
    return 0


def cmd_inventory(args, st: Settings) -> int:
    eng = Engine(st)
    inv = eng.source.inventory()
    _hr("V1 HISTORICAL SUBSTRATE")
    if not inv.get("available"):
        print(f"  not found at {inv.get('path')}")
        return 1
    for k in ("wallet_trades", "raw_events", "wallets", "markets", "tokens",
              "resolutions", "tape_days"):
        _kv(k, _fmt(inv.get(k)))
    _kv("resolutions missing settled_ts",
        f"{inv['resolutions_missing_settled_ts']:,} of {inv['resolutions']:,}")
    _kv("settled_ts coverage", f"{inv['settled_ts_coverage']:.1%}")

    from .ingest.settled_ts import coverage
    cov = coverage(eng.store)
    _hr("V3 SETTLEMENT TIMESTAMPS")
    _kv("recorded", _fmt(cov["total"]))
    _kv("usable (confidence >= 0.60)", _fmt(cov["usable"]))
    _kv("by method", json.dumps(cov["by_method"]))
    print(f"\n  {cov['note']}")

    _hr("V3 STORE")
    for t in ("markets", "book_snapshots", "news_items", "chain_events",
              "decisions", "fills", "positions", "strategies",
              "loss_forensics", "missed_opportunities"):
        _kv(t, _fmt(eng.store.count(t)))
    return 0


def cmd_dna(args, st: Settings) -> int:
    eng = Engine(st)
    if not eng.source.available:
        print("no historical tape; cannot build wallet DNA")
        return 1
    from .intelligence.wallets import WalletIntelligence, cohorts, rank
    t0 = time.perf_counter()
    wi = WalletIntelligence(st, eng.source)
    dna = wi.build(max_wallets=args.max_wallets, min_trades=args.min_trades)
    if not dna:
        print("no wallet met the evidence floor")
        return 1
    _hr(f"WALLET DNA — {len(dna)} profiles in "
        f"{time.perf_counter() - t0:.1f}s")
    print(f"  {'wallet':<20} {'alpha':>9} {'win':>7} {'exp':>9} {'PF':>7} "
          f"{'maxDD':>7} {'n':>6} {'mkts':>5}  evidence")
    for d in rank(dna, by="alpha")[:args.top]:
        pf = "inf" if d.profit_factor == float("inf") else f"{d.profit_factor:.2f}"
        print(f"  {d.wallet[:18]:<20} {d.alpha_vs_band:>+9.4f} "
              f"{d.win_rate:>6.1%} {d.expectancy:>+9.4f} {pf:>7} "
              f"{d.max_drawdown:>6.1%} {d.trades:>6} {d.markets:>5}  "
              f"{d.evidence_quality}")
    _hr("COHORTS")
    for k, v in cohorts(dna).items():
        _kv(k, f"{len(v)} wallet(s)")
    n_alpha = sum(1 for d in dna.values() if d.alpha_vs_band > 0)
    print(f"\n  {n_alpha} of {len(dna)} wallets show positive alpha over their "
          f"own price band.\n  Win rate alone would rank "
          f"{sum(1 for d in dna.values() if d.win_rate > 0.6)} wallets as "
          f"good; most of that is\n  the favourite-longshot bias, not skill.")
    return 0


def cmd_scan(args, st: Settings) -> int:
    eng = Engine(st)
    eng.start(build_dna=not args.no_dna, max_wallets=args.max_wallets)
    s = eng.last_scan
    if not s:
        print("scanner produced nothing (no tape?)")
        return 1
    _hr("SCAN")
    _kv("markets scanned", _fmt(s.markets_scanned))
    _kv("eligible after stage 1", _fmt(s.markets_eligible))
    _kv("ranked", _fmt(len(s.opportunities)))
    _kv("dropped below the cut", _fmt(s.markets_dropped_at_stage1))
    _kv("elapsed", f"{s.elapsed_ms} ms")
    for n in s.notes:
        print(f"\n  note: {n}")
    _hr("TOP OPPORTUNITIES")
    print(f"  {'score':>7} {'mkt':>6} {'fair':>6} {'edge':>7} {'exec':>5} "
          f" question")
    for o in s.opportunities[:args.top]:
        print(f"  {o.overall_score:>7.4f} {o.market_probability:>6.3f} "
              f"{o.fair_probability:>6.3f} {o.edge:>+7.3f} "
              f"{o.execution_score:>5.2f}  {o.question[:56]}")

    if args.decide and s.opportunities:
        _hr(f"FULL DECISION on the top {min(args.decide, len(s.opportunities))}")
        print("  Each candidate is evaluated at the moment of its own most "
              "recent print,\n  which is the event-driven anchor an honest "
              "backtest uses. Every layer is\n  still built with `<= as_of`, "
              "so no candidate can see its own future.\n")
        for o in s.opportunities[:args.decide]:
            d = eng.decision.decide(market_id=o.market_id, token_id=o.token_id,
                                    as_of=o.as_of, wallet_dna=eng.wallet_dna)
            print(f"\n  {o.question[:66]}")
            _kv("action", d.action)
            _kv("blocking gate", d.blocking_gate or "none")
            _kv("fair / market", f"{d.fair_probability:.4f} / "
                                 f"{d.market_probability:.4f}")
            _kv("edge", f"{d.edge:+.4f}")
            _kv("confidence", f"{d.confidence:.4f}")
            if d.sizing:
                _kv("sizing", f"{d.sizing.feasibility.value}: "
                              f"${d.sizing.size_usdc:.2f}")
                if not d.sizing.ok:
                    print(f"        {d.sizing.reason}")
            if d.debate:
                st_ = d.debate.stats
                _kv("agents", f"{st_['n_active']} active, "
                              f"{st_['n_abstained']} abstained, "
                              f"consensus {d.debate.consensus:.2f}, "
                              f"disagreement {d.debate.disagreement:.2f}")
            if d.gates:
                for g in d.gates.blocking:
                    print(f"        [{g.gate}] {g.reason}")
    return 0


def cmd_decide(args, st: Settings) -> int:
    eng = Engine(st)
    eng.start(build_dna=True, max_wallets=args.max_wallets)
    d = eng.decision.decide(market_id=args.market, token_id=args.token or "",
                            wallet_dna=eng.wallet_dna)
    print(json.dumps(d.to_dict(), indent=2, default=str))
    return 0


def cmd_collect(args, st: Settings) -> int:
    if args.enable:
        st.collectors.enabled = True
    eng = Engine(st)
    if args.backfill_settled:
        from .ingest.settled_ts import SettlementTimeCollector
        c = SettlementTimeCollector(st, eng.store)
        c.run()
        out = c.backfill(limit=args.limit)
        _hr("SETTLEMENT TIME BACKFILL")
        print(json.dumps(out, indent=2))
        return 0
    from .ingest.collectors import run_all
    _hr("COLLECTORS")
    for r in run_all(st, eng.store):
        print(f"  {r.collector:<14} {r.status:<16} {r.elapsed_ms:>6}ms  "
              f"{r.detail or r.error}")
        for n in r.notes:
            print(f"      note: {n}")
    if not st.collectors.enabled:
        print("\n  Collectors are DISABLED. Nothing dialled out.")
        print("  Enable with `pqv3 collect --enable`, or set it in config.")
    return 0


def cmd_forensics(args, st: Settings) -> int:
    eng = Engine(st)
    losses = eng.forensics.run_all_losses()
    missed = eng.forensics.analyse_missed()
    _hr("FORENSICS")
    _kv("new loss records", _fmt(len(losses)))
    _kv("new missed-opportunity records", _fmt(len(missed)))
    for r in losses[:10]:
        print(f"\n  {r.classification} (remedy: {r.remedy})")
        print(f"    {r.narrative}")
    _hr("GATE COST")
    rows = eng.forensics.gate_cost_report()
    if not rows:
        print("  no evaluable rejections yet — a gate's cost can only be "
              "measured\n  once the markets it declined have resolved")
    for r in rows:
        print(f"  {r['gate']:<26} n={r['n']:<5} correct={r['saved']:<5} "
              f"missed={r['missed']:<5} net={r['net']:+.3f}  {r['verdict']}")
    return 0


def cmd_agents(args, st: Settings) -> int:
    from .agents.registry import catalogue
    _hr("AGENTS")
    for a in catalogue():
        tag = " [adversarial]" if a["adversarial"] else ""
        req = ", ".join(a["requires"]) or "—"
        print(f"  {a['number']:>2}. {a['name']:<26}{tag}")
        print(f"      {a['role']}")
        print(f"      requires: {req}")
    return 0


def cmd_gates(args, st: Settings) -> int:
    from .decision.gates import gate_catalogue
    _hr("THE TWELVE GATES")
    for g in gate_catalogue():
        print(f"  {g['gate']:<26} owner={g['owner']:<16} "
              f"critical={g['critical']}")
        print(f"      {g['rationale']}")
    return 0


def cmd_capital(args, st: Settings) -> int:
    """Prove the $100 model behaves at $100, and say where it breaks."""
    from .portfolio.capital import Account, CapitalEngine
    eng = CapitalEngine(st)
    acct = Account(starting_capital=st.capital.starting_capital,
                   cash=st.capital.starting_capital,
                   peak_equity=st.capital.starting_capital)
    _hr(f"CAPITAL MODEL AT ${st.capital.starting_capital:.2f}")
    _kv("equity", f"${acct.equity:.2f}")
    _kv("reserve", f"${acct.equity * st.capital.reserve_fraction:.2f} "
                   f"({st.capital.reserve_fraction:.0%})")
    _kv("risk capital", f"${acct.risk_capital(st.capital):.2f}")
    _kv("per-trade cap", f"${acct.equity * st.capital.max_fraction_per_trade:.2f} "
                         f"({st.capital.max_fraction_per_trade:.0%})")
    _kv("venue minimum order", f"${st.capital.min_order_usdc:.2f}")
    _kv("venue minimum shares", f"{st.capital.min_shares:g}")

    _hr("SIZING ACROSS THE PRICE CURVE")
    print(f"  {'price':>6} {'p_est':>6} {'liq':>9} {'result':<22} "
          f"{'size':>8} {'shares':>8}  reason")
    for price in (0.05, 0.15, 0.30, 0.50, 0.70, 0.85, 0.95):
        for liq in (20.0, 500.0):
            r = eng.size(account=acct, probability=min(0.99, price + 0.08),
                         signal_price=price, available_liquidity=liq,
                         confidence=0.8, correlation_key="test")
            print(f"  {price:>6.2f} {min(0.99, price + 0.08):>6.2f} "
                  f"{liq:>9.2f} {r.feasibility.value:<22} "
                  f"${r.size_usdc:>7.2f} {r.size_shares:>8.2f}  "
                  f"{r.reason[:60]}")
    print("\n  CAPITAL_INFEASIBLE rows are the honest answer, not a bug: at "
          "$100 of\n  equity a percentage cap collides with absolute venue "
          "minimums. A strategy\n  that only works above a size we cannot "
          "deploy is not a strategy we have.")
    return 0


def _print_turn(r: dict) -> None:
    _hr(f"{r['mode']}  ·  state {r['state']}  ·  {r.get('elapsed_ms', 0)} ms")
    print(f"  read as {r['mode']}: {r['mode_reason']}")
    if r.get("topics"):
        print(f"  topics: {', '.join(r['topics'])}")
    print()
    for line in r.get("finding", []):
        print(f"  {line}")
    for c in r.get("cannot", []):
        print(f"\n  CANNOT — {c['capability']}")
        print(f"    {c['detail']}")
        if c.get("workaround"):
            print(f"    instead: {c['workaround']}")
        print(f"    (authorised by {c['charter']})")
    doc = r.get("document") or {}
    if doc:
        _hr("DOCUMENT")
        _kv("path", doc.get("path"))
        _kv("read", f"{doc.get('words', 0):,} words, "
                    f"{doc.get('chars', 0):,} chars")
        kinds: dict = {}
        for c in doc.get("claims", []):
            kinds[c["kind"]] = kinds.get(c["kind"], 0) + 1
        _kv("statements", ", ".join(f"{k.lower()} {v}"
                                    for k, v in sorted(kinds.items())) or "none")
        for c in doc.get("claims", [])[:10]:
            mark = "testable" if c["testable"] else "not testable here"
            print(f"\n  [{c['kind']}] {c['text'][:150]}")
            print(f"      -> {mark}"
                  + (f"; columns: {', '.join(c['features'])}"
                     if c["features"] else ""))
            if c.get("caveat"):
                print(f"      !! {c['caveat']}")
        if doc.get("proposals"):
            _hr("CANDIDATES (untested — they enter the ordinary pass)")
            for pr in doc["proposals"][:20]:
                src = "stated" if "stated in" in pr["direction_source"] \
                    else "unstated, so both directions are tested"
                print(f"  {pr['statement']:<44} direction {src}")
            print("\n  No threshold is taken from the document. Each candidate "
                  "is swept over the\n  standard quantile grid (0.20 / 0.35 / "
                  "0.50 / 0.65 / 0.80), and every variant\n  counts against "
                  "the multiple-comparison denominator — a number lifted from\n"
                  "  prose has no denominator behind it.")
        if doc.get("missing_data"):
            _hr("NO OBSERVATION COLUMN FOR THIS")
            for md in doc["missing_data"]:
                print(f"  {md['concept']}")
                print(f"      {md['why']}")

    surf = r.get("surfaced") or []
    if surf:
        _hr("NOTICED SINCE YOU LAST ASKED")
        for s in surf:
            print(f"  [{s.get('kind')}] {s.get('headline')}")
            print(f"      {s.get('measured')}")
            if s.get("why"):
                print(f"      {s['why']}")
            print(f"      priority {float(s.get('priority') or 0):.3f} "
                  f"(estimate: importance x impact x urgency)")

    diag = r.get("diagnosis") or []
    if diag:
        _hr("DIAGNOSIS")
        for d in diag[:14]:
            if d.get("severity"):
                print(f"  {d['severity']:<9} {d['finding']}")
                print(f"            measured: {d['measured']}")
                print(f"            {d['why_it_matters']}")
            elif d.get("root_cause"):
                rc = d["root_cause"]
                print(f"  {d['topic']}: first break at {rc['layer']} — "
                      f"{rc['check']}")
                print(f"    {rc['detail']}")
                if rc.get("fix"):
                    print(f"    fix: {rc['fix']}")
    chains = [p for p in (r.get("plan") or []) if p.get("chain")]
    for p in chains:
        _hr(f"{p['topic'].upper()}  INPUT -> ... -> UI")
        for ln in p["chain"]:
            mark = "ok" if ln["ok"] else "BREAK"
            print(f"  [{mark:<5}] {ln['layer']:<11}{ln['check']}")
            print(f"           {ln['detail']}")
            if ln.get("fix") and not ln["ok"]:
                print(f"           fix: {ln['fix']}")
        if not p["root_cause"]:
            print(f"  {p['note']}")

    steps = [p for p in (r.get("plan") or []) if p.get("phase")]
    if steps:
        _hr("PLAN")
        for s in steps:
            mark = "  (NOT AVAILABLE)" if s.get("blocked") else ""
            print(f"  {s['step']}. {s['phase']:<11}{s['detail']}{mark}")
            for f in s.get("files", [])[:12]:
                print(f"       {f['path']}  ({f['lines']} lines)")
    acts = r.get("actions") or []
    if acts:
        _hr("ACTIONS")
        for a in acts[:14]:
            flag = "" if a["runnable"] else "   [human only]"
            print(f"  {a['command']:<34}{a['effect'][:60]}{flag}")
            if a.get("why_not"):
                print(f"       {a['why_not']}")
    ran = r.get("ran") or {}
    if ran.get("action"):
        _hr(f"RAN {ran['action']['command']}  exit={ran.get('exit_code')}")
        print(ran.get("output") or ran.get("error") or "")
    llm = r.get("llm") or {}
    if llm.get("available"):
        _hr("NARRATION (commentary only)")
        print("  " + (llm.get("text") or "").replace("\n", "\n  "))
    elif llm.get("note"):
        print(f"\n  {llm['note']}")
    if r.get("charter"):
        print(f"\n  charter: {' · '.join(r['charter'])}")


def cmd_chat(args, st: Settings) -> int:
    """The console, from a terminal. Same code path as the dashboard's CHAT."""
    from .agents.console import Console
    from .core.store import Store

    store = Store(st.ensure_dirs())
    engine = None
    if not args.no_engine:
        try:
            engine = Engine(st)
            engine.start(build_dna=args.dna)
        except Exception as e:                                # noqa: BLE001
            print(f"  engine unavailable ({type(e).__name__}: {e}); "
                  f"answering from the store alone")
    con = Console(st, engine.store if engine else store, engine=engine)

    if args.question:
        _print_turn(con.ask(" ".join(args.question), run=args.run,
                            confirm=args.confirm,
                            narrate=not args.no_narrate))
        return 0

    _hr("POLYMARKET QUANT BRIDGE — CONSOLE")
    print("  Ask anything, or give an instruction. Ctrl-C or 'exit' to leave.")
    print("  Try: audit the entire system · why is the news panel empty ·")
    print("       what should I do next · analyse every wallet")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if q.lower() in ("exit", "quit", "q"):
            return 0
        if not q:
            continue
        _print_turn(con.ask(q, narrate=not args.no_narrate))


def cmd_ingest(args, st: Settings) -> int:
    """§29. Read a document, convert it into candidates, adopt nothing."""
    from .agents.console import Console
    from .core.store import Store

    con = Console(st, Store(st.ensure_dirs()))
    _print_turn(con.ask(f"analyse the document {args.path}", narrate=False))
    return 0


def cmd_watch(args, st: Settings) -> int:
    """§40. What the system noticed on its own, ranked."""
    from .agents.surface import PRIORITY_FLOOR, Surfacer
    from .core.store import Store

    s = Surfacer(st, Store(st.ensure_dirs()))
    if args.ack:
        n = s.ack()
        print(f"  acknowledged {n} discovery/discoveries")
        return 0
    fresh = s.run()
    _hr("SURFACED NOW")
    if not fresh:
        print("  nothing new above the floor. §33: that is an answer.")
    for d in fresh:
        print(f"  [{d['kind']}] {d['headline']}")
        print(f"      {d['measured']}")
        print(f"      {d['why']}")
        print(f"      priority {d['priority']:.3f} = importance "
              f"{d['importance']:.2f} x impact {d['impact']:.2f} x urgency "
              f"{d['urgency']:.2f}   [ESTIMATE]")
        if d.get("action"):
            print(f"      -> pqv3 {d['action'].removeprefix('pqv3 ')}")
    _hr("EVERYTHING RECORDED")
    rows = s.store.query(
        "SELECT kind, headline, measured, priority, surfaced, acked "
        "  FROM discoveries ORDER BY id DESC LIMIT 25")
    for r in rows:
        flag = "shown" if r["surfaced"] else "below floor"
        print(f"  {r['priority']:.3f}  {flag:<11} {r['headline'][:70]}")
    print(f"\n  the floor is {PRIORITY_FLOOR}. Below it a finding is recorded "
          f"but not\n  surfaced, because a monitor that interrupts over "
          f"everything gets muted.\n  The three factors are estimates that "
          f"order this queue; nothing else\n  reads them.")
    return 0


def _series_for(st: Settings, token: str, lookback_days: int) -> tuple:
    """(times, prices) for one token from the tape, oldest first."""
    from .core.source import HistoricalSource
    src = HistoricalSource(st)
    if not src.available:
        return (), ()
    as_of = src.latest_ts()
    # `prints` yields (ts, price, usdc, side) tuples, oldest first.
    rows = src.prints(token, as_of, lookback_secs=lookback_days * 86_400,
                      limit=20_000)
    return ([int(r[0]) for r in rows], [float(r[1]) for r in rows])


def cmd_depend(args, st: Settings) -> int:
    """§11. Nonlinear dependence between two token price series."""
    from .research import dependence as D

    ta, a = _series_for(st, args.token_a, args.days)
    tb, b = _series_for(st, args.token_b, args.days)
    if not a or not b:
        print("  no tape, or no prints for one of those tokens")
        return 2
    # Align on the shorter series by index, oldest first. Two tapes are not
    # sampled on the same clock; index alignment is an approximation and it is
    # named as one rather than presented as a join on time.
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    print(f"  aligned on {n} prints by index, not by timestamp — the two "
          f"tapes\n  do not share a clock, and this is an approximation")

    _hr("MUTUAL INFORMATION")
    r = D.mutual_information(a, b, draws=args.draws)
    _kv("verdict", r.verdict)
    _kv("nats (raw)", _fmt(r.nats, 5))
    _kv("null mean (the bias)", _fmt(r.surrogate.get("null_mean"), 5))
    _kv("excess", _fmt(r.surrogate.get("excess"), 5))
    _kv("p", r.surrogate.get("p_value"))
    _kv("stable across bins", r.stable)
    print(f"\n  {r.note}")
    for w in r.warnings:
        print(f"  ! {w}")

    _hr("TRANSFER ENTROPY, BOTH DIRECTIONS")
    both = D.transfer_entropy_both_ways(a, b, lag=args.lag, draws=args.draws)
    for k in ("a_to_b", "b_to_a"):
        d = both[k]
        print(f"  {k:<8} {d['verdict']:<24} "
              f"p={d['surrogate'].get('p_value')}")
    _kv("reading", both["reading"])
    print(f"\n  {both['note']}")

    _hr("LEAD-LAG")
    ll = D.lead_lag(a, b, max_lag=args.max_lag, draws=args.draws)
    _kv("verdict", ll.verdict)
    _kv("best lag (prints)", ll.best_lag)
    _kv("best |r|", _fmt(abs(ll.best_corr), 4))
    _kv("null mean best |r|", _fmt(ll.surrogate.get("null_mean"), 4))
    _kv("p", ll.surrogate.get("p_value"))
    print(f"\n  {ll.note}")
    return 0


def cmd_cycles(args, st: Settings) -> int:
    """§11. Periodicity on the tape's own irregular sampling."""
    from .research import spectral as S

    times, prices = _series_for(st, args.token, args.days)
    if not prices:
        print("  no tape, or no prints for that token")
        return 2
    r = S.analyse(times, prices, draws=args.draws)
    _hr(f"PERIODICITY — {args.token[:24]}")
    _kv("prints", r.n)
    _kv("span", f"{r.span_secs / 86400:.2f} d")
    _kv("verdict", r.verdict)
    _kv("best period", f"{r.best_period_hours:.3f} h")
    _kv("peak power", _fmt(r.best_power, 4))
    _kv("null mean peak", _fmt(r.surrogate.get("null_mean"), 4))
    _kv("p", r.surrogate.get("p_value"))
    if r.peaks:
        _hr("TOP PEAKS")
        for p in r.peaks:
            print(f"  {p['period_hours']:>10.3f} h   power {p['power']:.4f}")
    print(f"\n  {r.note}")
    for w in r.warnings:
        print(f"  ! {w}")
    return 0


def cmd_states(args, st: Settings) -> int:
    """§11/§19. Hidden-state model over a token's price series."""
    from .regime import hidden as H

    _times, prices = _series_for(st, args.token, args.days)
    if not prices:
        print("  no tape, or no prints for that token")
        return 2
    print(f"  fitting {len(prices)} prints — this is Baum-Welch in pure "
          f"Python\n  with a surrogate sweep; expect tens of seconds")
    r = H.analyse(prices, surrogates=args.surrogates)
    _hr(f"HIDDEN STATES — {args.token[:24]}")
    _kv("verdict", r.verdict)
    _kv("states chosen (BIC)", r.best_states)
    _kv("restart agreement", r.restart_agreement)
    if r.surrogate:
        _kv("stage 1 (vs reordered)", f"p={r.surrogate.get('p_value')}")
    if r.beyond_short_memory:
        b = r.beyond_short_memory
        _kv("stage 2 (vs Markov-1)",
            f"BIC advantage {b.get('bic_advantage')}")
    if r.persistence:
        p = r.persistence
        _kv("persistence ratio",
            f"{p.get('persistence_ratio')} — leans "
            f"{'switching' if p.get('leans_switching') else 'smooth latent'} "
            f"(DIAGNOSTIC ONLY, gates nothing)")
        print(f"\n  {p.get('reliability')}")
        print(f"  cannot: {p.get('what_this_method_cannot_do')}")
    if r.model:
        _kv("expected durations", r.model.get("expected_durations"))
        _kv("stationary occupancy", r.model.get("stationary"))
    if r.bic_by_states:
        _hr("MODEL SELECTION")
        for row in r.bic_by_states:
            print(f"  {row['states']} states  BIC {row['bic']:>12.2f}  "
                  f"loglik {row['loglik']:>10.2f}  params {row['n_params']:>3}"
                  f"  agreed {row['restart_agreement']}/{row['restarts']}")
    if r.state_path_summary:
        _hr("STATES")
        for s in r.state_path_summary:
            print(f"  state {s['state']}  share {s.get('share')}  "
                  f"mean {_fmt(s.get('mean_value'), 4)}  "
                  f"sd {_fmt(s.get('sd_value'), 4)}  "
                  f"mean run {s.get('mean_run_length')}")
    print(f"\n  {r.note}")
    for w in r.warnings:
        print(f"  ! {w}")
    return 0


def cmd_montecarlo(args, st: Settings) -> int:
    """§17/§1. Sequencing risk: the same trades in a different order."""
    import json as _json

    from .core.store import Store
    from .research import montecarlo as MC

    store = Store(st.ensure_dirs())
    rows = store.query(
        "SELECT strategy_id, params FROM strategies WHERE strategy_id=?",
        (args.strategy_id,))
    if not rows:
        print(f"  no strategy '{args.strategy_id}'. `pqv3 strategies` lists "
              f"them")
        return 2
    params = _json.loads(rows[0]["params"] or "{}")
    returns = (params.get("out_of_sample") or {}).get("returns") or []
    if not returns:
        returns = (params.get("in_sample") or {}).get("returns") or []
        if returns:
            print("  ! using IN-SAMPLE returns: this strategy carries no "
                  "stored\n    out-of-sample return series. Treat everything "
                  "below as optimistic")
    if not returns:
        print("  this strategy stored no per-trade return series, so there is "
              "nothing\n  to resample. Run `pqv3 backtest "
              f"{args.strategy_id}` first")
        return 2

    frac = args.fraction if args.fraction > 0 \
        else st.capital.max_fraction_per_trade
    cmp_ = MC.compare_resamplers(
        returns, starting_capital=st.capital.starting_capital,
        fraction=frac, paths=args.paths,
        hard_stop_drawdown=st.capital.hard_stop_drawdown)

    _hr(f"MONTE CARLO — {args.strategy_id}")
    _kv("trades", len(returns))
    _kv("bankroll", f"${st.capital.starting_capital:.2f}")
    _kv("risked per trade", f"{frac:.1%} of equity")
    _kv("paths per resampler", args.paths)
    print(f"\n  {'':<22}{'iid':>14}{'block':>14}")
    for label, key in (("median final", "median_final"),
                       ("5th percentile", "p05_final"),
                       ("95th percentile", "p95_final"),
                       ("prob profit", "prob_profit"),
                       ("median max DD", "median_max_drawdown"),
                       ("95th max DD", "p95_max_drawdown"),
                       ("prob hard stop", "prob_hard_stop"),
                       ("prob ruin", "prob_ruin")):
        print(f"  {label:<22}{cmp_['iid'][key]:>14}{cmp_['block'][key]:>14}")
    b = cmp_["block"]
    _hr("READING")
    print(f"  {cmp_['reading']}")
    print(f"\n  {b['note']}")
    for w in b.get("warnings", []):
        print(f"  ! {w}")
    return 0


def cmd_checkpoint(args, st: Settings) -> int:
    """§31. Change control: create, inspect, and plan a rollback."""
    from .core.checkpoint import Checkpoints, git_state
    from .core.store import Store

    cps = Checkpoints(st, Store(st.ensure_dirs()))

    if args.rollback:
        plan = cps.rollback_plan(args.rollback)
        if "error" in plan:
            print(f"  {plan['error']}")
            return 2
        _hr(f"ROLLBACK PLAN — {args.rollback}")
        _kv("command", plan["command"])
        if plan["code_changes"].get("stat"):
            print("\n  code changed since the checkpoint:")
            for line in str(plan["code_changes"]["stat"]).splitlines()[-20:]:
                print(f"    {line}")
        if plan["store_changes"]:
            print("\n  store changed since the checkpoint:")
            for k, v in plan["store_changes"].items():
                print(f"    {k:<22} {v['was']} -> {v['now']} "
                      f"({v['delta']:+d})")
        print(f"\n  {plan['warning']}")
        for b in plan["blockers"]:
            print(f"\n  BLOCKED: {b}")
        if not plan["safe"]:
            print("\n  refusing. Clear the blockers above first.")
            return 1
        if not args.yes:
            print("\n  this is a plan only. Re-run with --yes to execute the "
                  "command above,\n  or run it yourself — it is an ordinary "
                  "git command and nothing here is\n  hiding anything from "
                  "you.")
            return 0
        from .core.checkpoint import _git
        cps.create(label="pre-rollback", objective=f"before {args.rollback}")
        ok, out = _git(*plan["command"].split()[1:])
        print(f"\n  {'ok' if ok else 'FAILED'}: {out}")
        return 0 if ok else 1

    if args.list:
        rows = cps.list()
        _hr("CHECKPOINTS")
        if not rows:
            print("  none yet. `pqv3 checkpoint --note \"...\"` takes one.")
            return 0
        for r in rows:
            dirty = " [tree was dirty]" if r["git_dirty"] else ""
            print(f"  {r['checkpoint_id']}  {r['label']:<20} "
                  f"{(r['git_sha'] or '')[:12]:<14}{r['mode']:<10}{dirty}")
            if r["objective"]:
                print(f"      {r['objective']}")
        return 0

    if args.diff:
        d = cps.diff(args.diff)
        if "error" in d:
            print(f"  {d['error']}")
            return 2
        _hr(f"SINCE {args.diff}")
        for k, v in (d["store"] or {}).items():
            print(f"  {k:<22} {v['was']} -> {v['now']} ({v['delta']:+d})")
        if not d["store"]:
            print("  store: no table changed count")
        for line in str(d["code"].get("stat") or "").splitlines()[-20:]:
            print(f"  {line}")
        print(f"\n  {d['note']}")
        return 0

    g = git_state()
    cp = cps.create(label=args.label, objective=args.note, tests=args.tests)
    _hr(f"CHECKPOINT {cp.checkpoint_id}")
    _kv("label", cp.label)
    _kv("objective", cp.objective or "(none given)")
    _kv("git", f"{g.get('short', 'unavailable')} on "
               f"{g.get('branch', '?')}" + (" [DIRTY]" if g.get("dirty") else ""))
    _kv("mode", cp.mode)
    _kv("strategies live", cp.store.get("strategies_live"))
    _kv("rollback", cp.rollback)
    if g.get("dirty"):
        print(f"\n  {g['n_dirty']} uncommitted file(s). The SHA above does NOT "
              f"describe\n  what is currently running. Commit before relying "
              f"on this checkpoint.")
    return 0


def cmd_doctrine(args, st: Settings) -> int:
    """Print the charter in force, and the boundary it is held to."""
    from .agents import doctrine as doc
    from .core.store import Store

    limit = st.agents.llm_context_limit
    s = doc.status(limit)
    _hr("MASTER SYSTEM PROMPT")
    _kv("file", s["path"])
    _kv("available", s["available"])
    _kv("sections", s["sections"])
    _kv("size", f"{s['chars']:,} chars (~{s['approx_tokens']:,} tokens)")
    _kv("context limit", f"{s['context_limit']:,} tokens")
    _kv("in force", s["in_force"].upper())
    print(f"\n  {s['note']}")

    if args.full:
        print("\n" + (doc.charter() or doc.CONDENSED))
        return 0
    if args.section:
        sec = doc.section(args.section)
        if not sec:
            print(f"\n  no section {args.section}")
            return 2
        _hr(f"§{sec.number} {sec.title}")
        print(sec.body)
        return 0

    _hr("SECTIONS")
    for sec in doc.sections():
        print(f"  §{sec.number:<3} {sec.title}")

    try:
        store = Store(st.ensure_dirs())
    except Exception:                                         # noqa: BLE001
        store = None
    caps = doc.capabilities(st, store)
    _hr("WHAT THIS INSTALLATION CAN DO")
    for c in caps["can"]:
        print(f"  + {c['capability']}")
        print(f"      {c['detail']}")
        print(f"      ({c['charter']})")
    _hr("WHAT IT CANNOT")
    for c in caps["cannot"]:
        print(f"  - {c['capability']}")
        print(f"      {c['detail']}")
        if c.get("workaround"):
            print(f"      instead: {c['workaround']}")
        print(f"      ({c['charter']})")
    print(f"\n  {caps['note']}")
    return 0


def cmd_dashboard(args, st: Settings) -> int:
    from .server.app import Dashboard
    if args.port:
        st.server.port = args.port
    if args.host:
        st.server.host = args.host
    st.server.open_browser = not args.no_browser

    eng = Engine(st)
    print("starting engine…")
    steps = eng.start(build_dna=not args.no_dna, max_wallets=args.max_wallets)
    ok = sum(1 for s in steps if s.ok)
    print(f"  {ok}/{len(steps)} startup steps succeeded")
    if args.loops:
        eng.run_loops()
        print("  research and collector loops running")
    dash = Dashboard(st, eng)
    print(f"\n  dashboard: {st.server.url}\n  Ctrl-C to stop\n")
    try:
        dash.serve(block=True)
    finally:
        eng.stop()
    return 0


def cmd_mode(args, st: Settings) -> int:
    try:
        m = Mode(args.mode.upper())
    except ValueError:
        print(f"unknown mode. One of: {', '.join(x.value for x in Mode)}")
        return 2
    eng = Engine(st)
    if m is Mode.LIVE:
        print("LIVE mode cannot be set here. Use `pqv3 authorize-live`, which "
              "records\nthe system state at the moment of consent.")
        return 2
    eng.store.set_meta("mode", m.value)
    print(f"mode set to {m.value} (persisted)")
    return 0


def cmd_authorize_live(args, st: Settings) -> int:
    eng = Engine(st)
    eng.start(build_dna=False)
    from .server.api import Api
    api = Api(st, eng.store, eng)
    reqs = api._live_requirements()
    _hr("LIVE AUTHORIZATION REVIEW")
    for r in reqs:
        mark = "ok  " if r["met"] else "UNMET"
        print(f"  [{mark}] {r['requirement']:<48} {r['actual']}")
    unmet = [r for r in reqs if not r["met"]]
    ov = api.overview()
    _hr("CURRENT STATE")
    for k in ("mode", "account_value", "total_pnl", "win_rate",
              "completed_trades", "validated_strategies", "wallet_status"):
        _kv(k, _fmt(ov.get(k)))
    if not args.yes:
        print(f"\n  {len(unmet)} requirement(s) unmet. Nothing was changed.")
        print("  Re-run with --yes to authorize. This is a human decision "
              "the system\n  never makes for itself.")
        return 0
    out = eng.authorize_live(granted=True, actor=args.actor,
                            note=args.note or "")
    _hr("AUTHORIZED")
    print(json.dumps(out, indent=2))
    return 0


def cmd_selftest(args, st: Settings) -> int:
    _hr("SELFTEST")
    eng = Engine(st)
    checks = []

    checks.append(("store writable", eng.store.path.exists()))
    checks.append(("V1 tape reachable", eng.source.available))
    from .bootstrap import v2_status
    checks.append(("V2 package importable", v2_status()["available"]))
    from .secrets import wallet_configured
    checks.append(("wallet configured (optional)", wallet_configured()))
    checks.append(("live disabled", not st.live_authorized))

    # The capital model must be internally consistent at the configured
    # bankroll, or nothing downstream can size a trade.
    per_trade = st.capital.starting_capital * st.capital.max_fraction_per_trade
    checks.append((f"per-trade cap >= venue minimum "
                   f"(${per_trade:.2f} >= ${st.capital.min_order_usdc:.2f})",
                   per_trade >= st.capital.min_order_usdc))

    from .agents.registry import AGENTS
    checks.append((f"{len(AGENTS)} agents registered", len(AGENTS) == 25))
    from .decision.gates import GATES
    checks.append((f"{len(GATES)} gates registered", len(GATES) == 12))

    for name, ok in checks:
        print(f"  [{'ok ' if ok else 'no '}] {name}")
    hard = [n for n, ok in checks
            if not ok and n in ("store writable", "live disabled")]
    print(f"\n  {sum(1 for _, ok in checks if ok)}/{len(checks)} checks passed")
    return 1 if hard else 0



# ------------------------------------------------------- research commands
def cmd_discover(args, st: Settings) -> int:
    """The full pass: search -> screen -> out-of-sample -> ladder."""
    from .core.store import Store
    from .research import discover
    store = Store(st)
    res = discover.run(st, store, depth=args.depth,
                       screen_top=args.screen_top,
                       max_hypotheses=args.max_hypotheses,
                       rebuild_matrix=args.rebuild,
                       if_changed=args.if_changed,
                       progress=(lambda m: print(f"  {m}", flush=True))
                       if args.verbose else None)
    if res.skipped:
        _hr("DISCOVERY SKIPPED")
        for n in res.notes:
            print(f"  {n}")
        return 0
    _hr(f"DISCOVERY PASS {res.pass_id}")
    _kv("elapsed", f"{res.elapsed_secs}s")
    _kv("observations", _fmt(res.matrix.get("rows")))
    _kv("transformations defined", _fmt(res.tested))
    _kv("distinct hypotheses", _fmt(res.distinct_tested))
    _kv("survived in-sample screen", _fmt(res.screened))
    _kv("evaluated out-of-sample", _fmt(res.evaluated_oos))
    _kv("BH threshold", f"{res.bh_threshold:.4g}")
    _kv("cleared BH", _fmt(res.bh_significant))
    _kv("VALIDATED", _fmt(res.validated))
    _kv("DISTINCT findings", _fmt(res.distinct_findings))
    _hr("LADDER OUTCOMES")
    for k, v in sorted(res.by_status.items(), key=lambda kv: -kv[1]):
        _kv(k, _fmt(v))
    for n in res.notes:
        print(f"\n  note: {n}")
    if res.top:
        _hr("TOP CANDIDATES")
        for r in res.top[:args.top]:
            print(f"\n  [{r['status']}] {r['statement']}")
            print(f"      OOS n={r['oos_n']:,} across {r['oos_markets']} "
                  f"markets | expectancy {r['oos_expectancy']:+.4f}"
                  f" | matched excess {r['alpha_vs_baseline']:+.4f}"
                  f" | p={_p(r['p_value'])}")
            print(f"      ${st.capital.starting_capital:.0f} test: "
                  f"{r['capital_trades']} trades of {r['oos_n']:,} signals, "
                  f"{r['capital_return']:+.2%}")
            print(f"      {r['reason'][:150]}")
    else:
        print("\n  Nothing survived. That is a result, not a failure: the "
              "denominator\n  and every rejection are recorded in the store.")
    return 0


def _load_hypothesis(store, strategy_id):
    from .research.hypothesis import Hypothesis, Rule
    row = store.one("SELECT * FROM strategies WHERE strategy_id=? "
                    "ORDER BY version DESC LIMIT 1", (strategy_id,))
    if not row:
        return None, None
    params = json.loads(row["params"] or "{}")
    h = Hypothesis(row["strategy_id"], row["family"],
                   tuple(Rule(r["feature"], r["op"], r["value"])
                         for r in params.get("rules", [])),
                   params.get("statement", ""))
    return h, row


def cmd_backtest(args, st: Settings) -> int:
    from .core.store import Store
    from .research.matrix import build
    from .research.backtest import (capital_test, evaluate,
                                    settlement_clock_quality)
    store = Store(st)
    h, row = _load_hypothesis(store, args.strategy_id)
    if h is None:
        print(f"unknown strategy {args.strategy_id!r}. "
              f"List them with `pqv3 strategies`.")
        return 2
    m = build(st, store)
    split = m.split_ts(st.research.oos_fraction)
    lo, hi = m.index_range(split, 0)
    _hr(f"BACKTEST {h.hypothesis_id}")
    print(f"  {h.statement}\n")
    clock = settlement_clock_quality(m, lo, hi)
    if not clock["usable"]:
        print(f"  SETTLEMENT CLOCK UNUSABLE: {clock['reason']}\n")
    ev = evaluate(m, h, st, lo=lo, hi=hi)
    _hr("OUT-OF-SAMPLE (exact: payoff needs no order book)")
    for k in ("n", "n_comparable", "markets", "wallets", "expectancy",
              "alpha_vs_baseline", "baseline_expectancy", "win_rate",
              "profit_factor", "max_drawdown", "concentration", "p_value",
              "t_stat", "ci_low", "ci_high", "bootstrap_positive"):
        _kv(k, _fmt(getattr(ev, k)))
    ct = capital_test(m, h, st, lo=lo, hi=hi)
    _hr(f"${st.capital.starting_capital:.2f} CAPITAL TEST [{ct.hold_model}]")
    for k in ("starting_capital", "ending_capital", "total_return", "trades",
              "signals", "win_rate", "expectancy", "profit_factor",
              "max_drawdown", "capital_utilisation", "largest_position",
              "largest_win", "largest_loss", "fees_paid", "slippage_paid",
              "skipped_capital", "skipped_liquidity", "skipped_exposure"):
        _kv(k, _fmt(getattr(ct, k)))
    _kv("skip reasons", json.dumps(ct.skip_reasons))
    if ct.note:
        print(f"\n  {ct.note}")
    return 0


def cmd_walkforward(args, st: Settings) -> int:
    from .core.store import Store
    from .research import walkforward
    from .research.matrix import build
    store = Store(st)
    h, row = _load_hypothesis(store, args.strategy_id)
    if h is None:
        print(f"unknown strategy {args.strategy_id!r}")
        return 2
    m = build(st, store)
    wf = walkforward.run(m, h, st, schedule=args.schedule)
    _hr(f"WALK-FORWARD ({wf.schedule})")
    print(f"  {h.statement}\n")
    print(f"  {'fold':<6}{'n':>8}{'expectancy':>14}{'excess':>12}"
          f"{'win':>8}  positive")
    for f in wf.folds:
        print(f"  {f.index:<6}{f.n:>8,}{f.expectancy:>+14.5f}"
              f"{f.alpha_vs_baseline:>+12.5f}{f.win_rate:>8.1%}"
              f"  {'yes' if f.positive else 'no'}")
    print()
    _kv("evaluable folds", _fmt(wf.n_evaluable))
    _kv("positive share", _fmt(wf.positive_share))
    _kv("mean excess", _fmt(wf.mean_expectancy))
    _kv("worst fold", _fmt(wf.worst_fold))
    _kv("stable", _fmt(wf.stable))
    if wf.note:
        print(f"\n  {wf.note}")
    return 0


def cmd_strategies(args, st: Settings) -> int:
    from .core.store import Store
    store = Store(st)
    rows = store.query("SELECT * FROM strategies ORDER BY expectancy DESC "
                       "LIMIT ?", (args.limit,))
    if not rows:
        print("no strategies. Run `pqv3 discover`.")
        return 0
    _hr(f"STRATEGIES ({len(rows)})")
    for r in rows:
        p = json.loads(r["params"] or "{}")
        v = p.get("verdict", {})
        print(f"\n  {r['strategy_id']}  v{r['version']}  "
              f"[{r['status']}/{v.get('status')}]  {r['evidence_quality']}")
        print(f"    {p.get('statement', '')[:100]}")
        print(f"    n={r['trade_count']:,} win={r['win_rate']:.1%} "
              f"expectancy={r['expectancy']:+.5f} "
              f"pf={r['profit_factor']:.3f} maxdd={r['max_drawdown']:.1%}")
        if v.get("reason"):
            print(f"    {v['reason'][:150]}")
        for c in v.get("caveats", []):
            print(f"    CAVEAT: {c[:150]}")
    return 0


def cmd_promote(args, st: Settings) -> int:
    from .core.store import Store
    from .research.validate import promote
    store = Store(st)
    actor = "human" if args.yes else "system"
    out = promote(store, args.strategy_id, to=args.to.upper(), actor=actor,
                  note=args.note or "")
    _hr("PROMOTION")
    print(json.dumps(out, indent=2))
    if not out.get("ok") and "human" in str(out.get("error", "")):
        print("\n  Re-run with --yes to authorise this as a human decision.")
    return 0 if out.get("ok") else 1


def cmd_report(args, st: Settings) -> int:
    from .core.store import Store
    from .report import research_report
    store = Store(st)
    path = research_report.write(st, store)
    print(f"research report written to {path}")
    if args.show:
        print()
        print(research_report.build(st, store))
    return 0


def cmd_graph(args, st: Settings) -> int:
    from .core.source import HistoricalSource
    from .intelligence import graph as G
    from .intelligence.wallets import WalletIntelligence
    src = HistoricalSource(st)
    if not src.available:
        print("no tape")
        return 1
    dna = WalletIntelligence(st, src).build(max_wallets=args.max_wallets)
    g = G.build(st, src, wallets=list(dna))
    _hr("CROSS-WALLET GRAPH")
    _kv("nodes", _fmt(g.nodes))
    _kv("edges", _fmt(len(g.edges)))
    _kv("clusters", _fmt(len(g.clusters)))
    print(f"\n  {g.note}")
    if g.clusters:
        _hr("CLUSTERS")
        for c in g.clusters[:10]:
            print(f"  size={c.size} cohesion={c.cohesion:.3f} "
                  f"leader={c.leader[:14] or '-'}")
            print(f"    {c.note}")
    top = sorted(g.edges, key=lambda e: -e.coordination_score)[:12]
    if top:
        _hr("STRONGEST RELATIONSHIPS")
        for e in top:
            print(f"  {e.a[:12]} ~ {e.b[:12]}  {e.relationship:<16} "
                  f"coord={e.coordination_score:.3f} "
                  f"conf={e.confidence:.2f} shared={e.shared_markets}")
    return 0


def cmd_sequences(args, st: Settings) -> int:
    from .core.source import HistoricalSource
    from .intelligence import sequences as S
    src = HistoricalSource(st)
    if not src.available:
        print("no tape")
        return 1
    now = src.latest_ts()
    token = args.token
    if not token:
        for mk in src.active_markets(now, 30 * 86_400, 60):
            toks = src.tokens_for_market(mk["market_id"], now)
            if toks and toks[0]["prints"] >= 80:
                token = toks[0]["token_id"]
                break
    if not token:
        print("no token with enough prints")
        return 1
    rows = src.prints(token, now, lookback_secs=90 * 86_400, limit=4000)
    rep = S.analyse([p for _, p, _, _ in rows], [t for t, _, _, _ in rows])
    _hr(f"SEQUENCE ANALYSIS  token {token[:24]}")
    _kv("points", _fmt(rep.n))
    print(f"\n  {'test':<24}{'stat':>12}{'critical':>12}  structure")
    for t in rep.tests:
        print(f"  {t.name:<24}{t.statistic:>12.5f}{t.critical:>12.5f}"
              f"  {'YES' if t.passed else 'no'}")
        print(f"      {t.detail}")
    print()
    _kv("change points", _fmt(len(rep.change_points)))
    _kv("timing clusters", _fmt(len(rep.timing_clusters)))
    _kv("surprise (bits)", _fmt(rep.surprise))
    if rep.hidden_states:
        _kv("two-state fit", rep.hidden_states.get("interpretation"))
    print(f"\n  {rep.note}")
    return 0


def cmd_online(args, st: Settings) -> int:
    from .core.store import Store
    from .learning.online import OnlineLearner
    store = Store(st)
    ol = OnlineLearner(st, store)
    if args.reset:
        print(json.dumps(ol.reset(), indent=2))
        return 0
    if args.update:
        _hr("ONLINE UPDATE")
        print(json.dumps(ol.update(mode=st.mode.value).to_dict(), indent=2))
        return 0
    _hr("ONLINE LEARNING STATE")
    print(json.dumps(ol.report(mode=st.mode.value), indent=2))
    return 0



def cmd_signals(args, st: Settings) -> int:
    """Where candidates actually go — the funnel, not just 'no trade'."""
    from .core.store import Store
    from .research.matrix import build
    from .scanner.signals import StrategyMatcher, live_gap
    store = Store(st)
    matcher = StrategyMatcher(st, store)

    _hr("VALIDATED STRATEGIES LOADED")
    if not matcher.available:
        print("  none. A strategy must be VALIDATED and at PAPER or beyond.")
        print("  Run `pqv3 discover`, then `pqv3 strategies`.")
        return 0
    for h, rec in matcher.strategies[:args.top]:
        print(f"  {rec['strategy_id']}  [{rec['status']}]  "
              f"{rec['evidence_quality']}")
        print(f"    {rec['statement'][:96]}")

    m = build(st, store)
    split = m.split_ts(st.research.oos_fraction)
    lo, hi = m.index_range(split, 0)

    _hr("PIPELINE FUNNEL (out-of-sample window)")
    f = matcher.funnel(m, lo=lo, hi=hi)
    for k in ("observations", "strategies_loaded", "selected",
              "selection_rate", "distinct_markets", "distinct_wallets"):
        _kv(k, _fmt(f.get(k)))
    if f.get("note"):
        print(f"\n  {f['note']}")
    if f.get("by_strategy"):
        print()
        for sid, n in f["by_strategy"].items():
            print(f"    {sid}  fired {n:,} times")

    sigs = matcher.match(m, lo=lo, hi=hi, limit=args.limit)
    _hr(f"MOST RECENT SIGNALS ({len(sigs)})")
    for sg in sigs[:args.limit]:
        print(f"  ts={sg.ts}  px={sg.price:.4f}  wallet={sg.wallet[:12]} "
              f"market={sg.market_id[:16]}")
        print(f"    via {sg.strategy_id}: {sg.statement[:80]}")

    if args.decide and sigs:
        from .decision.decide import DecisionEngine
        from .intelligence.wallets import WalletIntelligence
        from .core.source import HistoricalSource
        src = HistoricalSource(st)
        dna = WalletIntelligence(st, src).build(max_wallets=40)
        eng = DecisionEngine(st, store, source=src)
        _hr(f"FULL DECISION on the top {min(args.decide, len(sigs))} signals")
        print("  These now carry a VALIDATED strategy record, so the research")
        print("  gates are answering a question about evidence that covers")
        print("  them - which was never true before.\n")
        for sg in sigs[:args.decide]:
            d = eng.decide(market_id=sg.market_id, token_id=sg.token_id,
                           as_of=sg.ts, strategy=sg.strategy, wallet_dna=dna)
            print(f"  {sg.statement[:70]}")
            _kv("action", d.action)
            _kv("blocking gate", d.blocking_gate or "none")
            if d.gates:
                for g in d.gates.blocking:
                    print(f"        [{g.gate}] {g.reason[:110]}")
            print()

    _hr("WHAT STANDS BETWEEN THIS AND LIVE")
    gap = live_gap(st, store)
    for b in gap["blocks_for_live"]:
        print(f"  [{'ok ' if b['have'] else 'NO '}] {b['item']}")
        print(f"        {b['detail']}")
    print(f"\n  {gap['note']}")
    return 0



def cmd_invert(args, st: Settings) -> int:
    """Are the gates protecting the account, or throwing money away?"""
    from .core.store import Store
    from .research import inversion
    store = Store(st)
    res = inversion.run(st, store, window=args.window,
                        include_gates=not args.strategies_only,
                        include_strategies=not args.gates_only,
                        progress=(lambda m: print(f"  {m}", flush=True))
                        if args.verbose else None)
    _hr(f"SIGNAL INVERSION PASS {res.pass_id}")
    _kv("conditions interrogated", _fmt(res.conditions))
    _kv("interpretation tests", _fmt(res.tests))
    _kv("BH threshold", f"{res.bh_threshold:.4g}")
    _kv("elapsed", f"{res.elapsed_secs}s")
    for n in res.notes:
        print(f"\n  note: {n}")

    if res.by_verdict:
        _hr("VERDICTS")
        for k, v in sorted(res.by_verdict.items(), key=lambda kv: -kv[1]):
            _kv(k, _fmt(v))

    _hr("CONDITION BY CONDITION")
    for v in res.verdicts[:args.top]:
        o = v.readings.get("ORIGINAL")
        i = v.readings.get("INVERTED")
        print(f"\n  [{v.verdict}] {v.condition}")
        print(f"    {v.statement[:96]}")
        if o and i:
            print(f"    {'':14}{'n':>8}{'expectancy':>13}{'matched':>11}"
                  f"{'win':>8}{'p':>12}")
            for nm, r in (("as signalled", o), ("inverted", i)):
                print(f"    {nm:<14}{r.n:>8,}{r.expectancy:>+13.5f}"
                      f"{r.matched_excess:>+11.5f}{r.win_rate:>8.1%}"
                      f"{_p(r.p_value):>12}")
        print(f"    -> {v.block_assessment}")
        print(f"       {v.detail[:220]}")

    _hr("WHAT THIS DOES AND DOES NOT SHOW")
    print("  Outcomes are never edited. 'Inverted' buys the COMPLEMENT")
    print("  contract, which is a real instrument with an exact payoff, and")
    print("  pays the same costs. Each side is compared only against other")
    print("  positions in its own price band and week, so an inversion cannot")
    print("  score well merely by turning longshots into favourites.")
    print()
    print("  A verdict of BLOCK_TOO_STRICT is a research finding, not an")
    print("  instruction to unblock. It becomes a strategy only by going")
    print("  through `pqv3 discover` like anything else.")
    return 0


# ------------------------------------------------------------------- parser
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pqv3", description="Polymarket Quant Bridge V3")
    p.add_argument("--capital", type=float, default=None,
                   help="starting capital (default 100.00)")
    p.add_argument("--mode", dest="run_mode", default=None,
                   help="RESEARCH|BACKTEST|WALK_FORWARD|SHADOW|PAPER")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, **kw):
        s = sub.add_parser(name, **kw)
        s.set_defaults(fn=fn)
        return s

    s = add("startup", cmd_startup, help="run the 15-step startup sequence")
    s.add_argument("--no-dna", action="store_true")
    s.add_argument("--max-wallets", type=int, default=120)

    add("inventory", cmd_inventory, help="what evidence actually exists")

    s = add("dna", cmd_dna, help="build wallet behavioural fingerprints")
    s.add_argument("--max-wallets", type=int, default=200)
    s.add_argument("--min-trades", type=int, default=60)
    s.add_argument("--top", type=int, default=25)

    s = add("scan", cmd_scan, help="scan every eligible market")
    s.add_argument("--top", type=int, default=20)
    s.add_argument("--decide", type=int, default=0,
                   help="run full decisions on the top N")
    s.add_argument("--no-dna", action="store_true")
    s.add_argument("--max-wallets", type=int, default=120)

    s = add("decide", cmd_decide, help="full decision for one market")
    s.add_argument("market")
    s.add_argument("--token", default="")
    s.add_argument("--max-wallets", type=int, default=120)

    s = add("collect", cmd_collect, help="run the live collectors")
    s.add_argument("--enable", action="store_true",
                   help="enable network collection for this run")
    s.add_argument("--backfill-settled", action="store_true",
                   help="repair settlement timestamps from the venue")
    s.add_argument("--limit", type=int, default=500)

    add("forensics", cmd_forensics, help="classify losses and missed chances")
    add("agents", cmd_agents, help="list the 25 agents")
    add("gates", cmd_gates, help="list the 12 validity gates")
    add("capital", cmd_capital, help="prove the $100 model")

    s = add("dashboard", cmd_dashboard, help="local URL dashboard")
    s.add_argument("--port", type=int, default=0)
    s.add_argument("--host", default="")
    s.add_argument("--no-browser", action="store_true")
    s.add_argument("--no-dna", action="store_true")
    s.add_argument("--loops", action="store_true",
                   help="run the continuous research loop")
    s.add_argument("--max-wallets", type=int, default=120)

    s = add("mode", cmd_mode, help="set the operating mode")
    s.add_argument("mode")

    s = add("authorize-live", cmd_authorize_live,
            help="review and authorize live trading (human only)")
    s.add_argument("--yes", action="store_true")
    s.add_argument("--actor", default="human")
    s.add_argument("--note", default="")

    s = add("discover", cmd_discover, help="run the full discovery pass")
    s.add_argument("--depth", type=int, default=2)
    s.add_argument("--screen-top", type=int, default=120)
    s.add_argument("--max-hypotheses", type=int, default=20000)
    s.add_argument("--top", type=int, default=10)
    s.add_argument("--rebuild", action="store_true",
                   help="rebuild the observation matrix from the tape")
    s.add_argument("--if-changed", action="store_true",
                   help="skip if the inputs are identical to the last pass")
    s.add_argument("-v", "--verbose", action="store_true")

    s = add("backtest", cmd_backtest,
            help="backtest one strategy at your bankroll")
    s.add_argument("strategy_id")

    s = add("walkforward", cmd_walkforward, help="walk-forward one strategy")
    s.add_argument("strategy_id")
    s.add_argument("--schedule", default="expanding",
                   choices=("expanding", "rolling"))

    s = add("strategies", cmd_strategies, help="list discovered strategies")
    s.add_argument("--limit", type=int, default=25)

    s = add("promote", cmd_promote, help="move a strategy along the lifecycle")
    s.add_argument("strategy_id")
    s.add_argument("--to", required=True)
    s.add_argument("--yes", action="store_true",
                   help="authorise as a human (required for APPROVED/LIVE)")
    s.add_argument("--note", default="")

    s = add("report", cmd_report, help="write the full research report")
    s.add_argument("--show", action="store_true")

    s = add("graph", cmd_graph, help="cross-wallet relationship graph")
    s.add_argument("--max-wallets", type=int, default=80)

    s = add("sequences", cmd_sequences, help="sequence / order analysis")
    s.add_argument("--token", default="")

    s = add("online", cmd_online, help="online learning weights")
    s.add_argument("--update", action="store_true")
    s.add_argument("--reset", action="store_true")

    s = add("signals", cmd_signals,
            help="validated strategies -> candidates, with the funnel")
    s.add_argument("--top", type=int, default=8)
    s.add_argument("--limit", type=int, default=10)
    s.add_argument("--decide", type=int, default=0,
                   help="run full decisions on the top N signals")

    s = add("invert", cmd_invert,
            help="test whether blocking signals are actually predictive")
    s.add_argument("--window", default="oos", choices=("oos", "is"))
    s.add_argument("--top", type=int, default=12)
    s.add_argument("--gates-only", action="store_true")
    s.add_argument("--strategies-only", action="store_true")
    s.add_argument("-v", "--verbose", action="store_true")

    # The control console. §2/§39 make this the primary human interface, so it
    # is reachable without a browser as well as with one.
    s = add("chat", cmd_chat,
            help="the AI control console: ask anything, in plain English")
    s.add_argument("question", nargs="*",
                   help="ask once and exit; omit for an interactive session")
    s.add_argument("--run", default="",
                   help="also execute a catalogued action (see `pqv3 chat "
                        "'list actions'`)")
    s.add_argument("--confirm", default="",
                   help="must equal --run; execution fails closed without it")
    s.add_argument("--dna", action="store_true",
                   help="build wallet DNA first (slower, richer answers)")
    s.add_argument("--no-engine", action="store_true",
                   help="answer from the store alone, without starting the "
                        "engine")
    s.add_argument("--no-narrate", action="store_true",
                   help="skip the optional local-model narration")

    # §11 / §17 / §31 — the research methods added on top of the console.
    s = add("depend", cmd_depend,
            help="nonlinear dependence, transfer entropy and lead-lag "
                 "between two tokens")
    s.add_argument("token_a")
    s.add_argument("token_b")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--lag", type=int, default=1)
    s.add_argument("--max-lag", type=int, default=20)
    s.add_argument("--draws", type=int, default=400)

    s = add("cycles", cmd_cycles,
            help="periodicity on the tape's own irregular sampling")
    s.add_argument("token")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--draws", type=int, default=200)

    s = add("states", cmd_states,
            help="hidden-state (HMM) model of a price series")
    s.add_argument("token")
    s.add_argument("--days", type=int, default=30)
    s.add_argument("--surrogates", type=int, default=24)

    s = add("montecarlo", cmd_montecarlo,
            help="sequencing risk: the same trades in a different order")
    s.add_argument("strategy_id")
    s.add_argument("--paths", type=int, default=2000)
    s.add_argument("--fraction", type=float, default=0.0,
                   help="equity risked per trade (default: the capital cap)")

    s = add("checkpoint", cmd_checkpoint,
            help="change control: record a rollback point, or plan a rollback")
    s.add_argument("--label", default="")
    s.add_argument("--note", default="", help="the objective for the work "
                                              "this checkpoint precedes")
    s.add_argument("--tests", default="", help="test result at this point")
    s.add_argument("--list", action="store_true")
    s.add_argument("--diff", default="", metavar="CHECKPOINT_ID")
    s.add_argument("--rollback", default="", metavar="CHECKPOINT_ID")
    s.add_argument("--yes", action="store_true",
                   help="execute the rollback command, not just plan it")

    s = add("ingest", cmd_ingest,
            help="read a research document and convert it into candidates")
    s.add_argument("path")

    s = add("watch", cmd_watch,
            help="what the system noticed on its own, ranked")
    s.add_argument("--ack", action="store_true",
                   help="mark everything currently surfaced as seen")

    s = add("doctrine", cmd_doctrine,
            help="the master system prompt in force, and its real boundary")
    s.add_argument("--full", action="store_true", help="print the whole text")
    s.add_argument("--section", type=int, default=0,
                   help="print one numbered section")

    add("selftest", cmd_selftest, help="check the installation")
    return p


def main(argv=None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    st = load()
    if args.capital:
        st.capital.starting_capital = args.capital
    if args.run_mode:
        try:
            st.mode = Mode(args.run_mode.upper())
        except ValueError:
            print(f"unknown mode {args.run_mode}")
            return 2
    if st.mode is Mode.LIVE and not st.live_authorized:
        print("LIVE requested without authorization; falling back to PAPER")
        st.mode = Mode.PAPER
    try:
        return args.fn(args, st)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
