"""
Command line entry point.

    python -m pqb.cli check      # validate config and the upstream reference
    python -m pqb.cli run        # start the loop (dry-run unless configured live)
    python -m pqb.cli status     # doubling state, last reconciliation, positions
    python -m pqb.cli kill       # halt new decisions (--flatten to also close out)
    python -m pqb.cli resume     # clear the kill switch
    python -m pqb.cli report     # aggregate the decision journal
    python -m pqb.cli wallets    # the dynamic wallet ranking, as derived
    python -m pqb.cli anomalies  # what the detectors have found, with evidence
    python -m pqb.cli research   # discover strategies through the Quant Bridge
    python -m pqb.cli calibration # is the edge real? evidence before live money
    python -m pqb.cli wallet 0x.. # reverse-engineer ONE wallet's method
    python -m pqb.cli backfill   # pull already-settled history to study now
    python -m pqb.cli playbook   # best exit rule for EVERY ranked wallet

``check`` performs no network I/O, so it is safe to run against a live config.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = "config/config.yaml"


def _load(args):
    from .config import load
    return load(args.config)


def _anomaly_kinds():
    from .analytics.anomalies import KINDS
    return KINDS


def _logger(cfg):
    from .logs import Log, setup
    setup(cfg.logging.level, cfg.log_path, cfg.logging.max_bytes,
          cfg.logging.backups)
    return Log()


# --- commands ---------------------------------------------------------------

def cmd_check(args) -> int:
    cfg = _load(args)
    print(f"config      : {cfg.source_path}")
    print(f"project root: {cfg.root}")
    try:
        from .upstream import upstream_root
        print(f"upstream    : {upstream_root()}  (ploymarketbot)")
    except Exception as exc:
        print(f"upstream    : NOT FOUND — {exc}")
        return 2

    from .upstream import clob_available
    ok, err = clob_available()
    print(f"py-clob     : {'available' if ok else f'unavailable ({err})'}")
    mode = "LIVE" if cfg.mode.live else "dry-run"
    print(f"mode        : {mode}")
    print(f"engine      : {cfg.engine.implementation}")
    print(f"markets     : {len(cfg.markets.track)} explicit, filters "
          f"min_liquidity={cfg.markets.filters.min_liquidity:g} "
          f"max={cfg.markets.filters.max_markets}")
    print(f"wallets     : {len(cfg.wallets)} pinned "
          f"(seeds the ranking; ingestion observes every wallet)")
    if cfg.intel.enabled:
        print(f"intel       : broad ingestion on, cohort={cfg.intel.cohort_size}, "
              f"lookback={cfg.intel.lookback_days:g}d, "
              f"global_sweep={cfg.intel.global_sweep}")
        print(f"anomalies   : {'on' if cfg.intel.anomalies.enabled else 'OFF'}, "
              f"{len(_anomaly_kinds())} detectors")
    else:
        print("intel       : DISABLED - no wallet ranking, no anomaly detection")

    from .quant import available as quant_available
    ok, why = quant_available()
    if ok:
        from .quant import bridge_root
        print(f"quant bridge: {bridge_root()}")
    else:
        print(f"quant bridge: NOT AVAILABLE - {why.splitlines()[0]}")
        print("              research is unavailable; the engine falls back to "
              "baseline scoring")

    strategies = cfg.data_dir / "strategies.json"
    if strategies.exists():
        from .research import load_strategies
        found = load_strategies(strategies)
        print(f"strategies  : {len(found)} discovered rule(s) at {strategies}")
    else:
        print("strategies  : none yet - run `python -m pqb.cli research` once "
              "features are captured")
    print(f"journal     : {cfg.journal_path}")
    print(f"progression : {len(cfg.doubling.progression)} steps, "
          f"first={cfg.doubling.progression[0]:g} "
          f"last={cfg.doubling.progression[-1]:g}")

    try:
        from .bridge.ports import load_engine
        load_engine(cfg.engine.implementation, engine_config=cfg.engine,
                    config=cfg)
        print("engine load : ok")
    except Exception as exc:
        print(f"engine load : FAILED — {exc}")
        return 2

    print(f"reconcile   : {'every loop' if cfg.reconciliation.every_loop else f'every {cfg.reconciliation.interval_seconds}s'}"
          f", halt on mismatch={cfg.reconciliation.halt_on_mismatch}")
    print(f"gate        : kill={'ARMED' if cfg.kill_switch_path.exists() else 'off'}"
          f"  halt={'ENGAGED' if cfg.halt_path.exists() else 'off'}")

    problems = cfg.validate()
    if problems:
        print("\nPROBLEMS:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nConfig is valid.")
    if cfg.mode.live:
        print("\n" + "!" * 72)
        print("!! THIS CONFIG IS ARMED FOR LIVE TRADING.")
        print("!!")
        print("!! FUNDING: Polymarket settles in USDC on POLYGON "
              f"(chain {cfg.polymarket.chain_id}).")
        print("!! USDC held on Ethereum mainnet must be BRIDGED to Polygon")
        print("!! first. An unbridged wallet reads as a zero balance and every")
        print("!! order fails, with nothing in the error naming the cause.")
        print("!!")
        print("!! KEY CUSTODY: the private key is read from the environment on")
        print("!! THIS machine only. Whoever controls this host and its")
        print("!! credentials effectively controls the wallet.")
        print("!" * 72)
    if args.show:
        print("\n" + json.dumps(cfg.redacted(), indent=2))
    return 0


def cmd_run(args) -> int:
    cfg = _load(args)
    problems = cfg.validate()
    if problems:
        for problem in problems:
            print(f"config error: {problem}", file=sys.stderr)
        return 1
    if args.dry_run:
        cfg.mode.dry_run = True
        cfg.mode.allow_live = False

    # Live trading needs an explicit, interactive confirmation on top of the two
    # config flags (§3). A no-console launch (the dashboard) has no tty to
    # confirm on, so live there must be pre-authorised with --yes-live rather
    # than started silently.
    if cfg.mode.live and not getattr(args, "yes_live", False):
        if sys.stdin is not None and sys.stdin.isatty():
            print("=" * 68)
            print("  LIVE TRADING — this will place REAL orders with REAL money.")
            print(f"  Account: {cfg.polymarket.funder_address or 'signer EOA'}")
            print("=" * 68)
            answer = input("  Type LIVE to confirm, anything else to abort: ")
            if answer.strip() != "LIVE":
                print("Aborted. Not starting live.")
                return 1
        else:
            print("error: live trading requires interactive confirmation.",
                  file=sys.stderr)
            print("  No console is attached, so it cannot be confirmed here.",
                  file=sys.stderr)
            print("  Re-run with --yes-live to authorise live explicitly, or",
                  file=sys.stderr)
            print("  use --dry-run for paper mode.", file=sys.stderr)
            return 1

    log = _logger(cfg)

    from .runner import BootAborted, Runner
    runner = Runner(cfg, log)

    async def main() -> int:
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def request_stop() -> None:
            stop.set()

        installed = False
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, AttributeError):
                loop.add_signal_handler(sig, request_stop)
                installed = True

        if not installed:
            # Windows. `loop.add_signal_handler` is not implemented on the
            # Proactor loop, so without this the only thing CTRL_BREAK does is
            # raise KeyboardInterrupt out of `asyncio.run` — which skips
            # `runner.stop()` entirely and kills the process mid-cycle.
            #
            # Routing the signal back onto the loop instead gives the same
            # graceful shutdown the Linux path gets: the runner finishes the
            # cycle it is in, persists its state and closes the journal.
            def on_signal(_signum, _frame):
                loop.call_soon_threadsafe(stop.set)

            for sig in (signal.SIGINT, getattr(signal, "SIGBREAK", None)):
                if sig is not None:
                    with contextlib.suppress(ValueError, OSError, AttributeError):
                        signal.signal(sig, on_signal)

        try:
            await runner.start()
        except BootAborted:
            # A Stop pressed during startup is a clean shutdown, not an
            # error — same exit the main loop's stop takes.
            with contextlib.suppress(Exception):
                await runner.stop()
            return 0
        except Exception as exc:
            log.error("Startup failed", error=f"{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                await runner.stop()
            return 1

        if args.cycles:
            for index in range(args.cycles):
                if stop.is_set():
                    break
                await runner.run_cycle()
                if index < args.cycles - 1:   # no idle wait after the last one
                    await asyncio.sleep(cfg.engine.cycle_seconds)
            await runner.stop()
            return 0

        worker = asyncio.create_task(runner.run_forever())
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({worker, waiter},
                                     return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            log.info("Stop requested — shutting down.")
        worker.cancel()
        waiter.cancel()
        for task in (worker, waiter):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await runner.stop()
        return 0

    try:
        return asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


def cmd_kill(args) -> int:
    cfg = _load(args)
    path = cfg.kill_switch_path
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "flatten" if args.flatten else "halt"
    path.write_text(body + "\n", encoding="utf-8")
    print(f"Kill switch armed ({body}): {path}")
    if args.flatten:
        print("The running process will close every open position.")
    else:
        print("New decisions are halted; existing positions are untouched.")
    print("Clear it with: python -m pqb.cli resume")
    return 0


def cmd_resume(args) -> int:
    """Clear both stops. A reconciliation halt is shown before it is cleared —
    clearing it is an assertion that the operator has looked at the divergence."""
    cfg = _load(args)
    cleared = 0

    halt = cfg.halt_path
    if halt.exists():
        try:
            detail = json.loads(halt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            detail = {}
        print("A RECONCILIATION HALT is in force:")
        print(f"  reason : {detail.get('reason', 'unknown')}")
        print(f"  summary: {detail.get('summary', '(none recorded)')}")
        result = detail.get("result") or {}
        for mismatch in (result.get("mismatches") or [])[:10]:
            print(f"    - {mismatch.get('kind')} {mismatch.get('subject', '')[:14]} "
                  f"expected={mismatch.get('expected')} "
                  f"actual={mismatch.get('actual')}")
        if not args.force:
            print("\nReview the divergence against the Polymarket UI first, then "
                  "re-run with --force to clear it.")
            return 1
        halt.unlink()
        cleared += 1
        print(f"Halt cleared: {halt}")

    kill = cfg.kill_switch_path
    if kill.exists():
        kill.unlink()
        cleared += 1
        print(f"Kill switch cleared: {kill}")

    if not cleared:
        print("Nothing to clear — trading is not stopped.")
    return 0


def cmd_status(args) -> int:
    cfg = _load(args)
    from .doubling import DoublingRule
    from .journal import Journal
    journal = Journal(cfg.journal_path)
    try:
        rule = DoublingRule(journal, cfg.doubling.progression,
                            cfg.doubling.multiple, cfg.doubling.enabled)
        print("-- doubling rule --")
        for key, value in rule.status().to_dict().items():
            print(f"  {key:14}: {value}")

        print("\n-- open lifecycles --")
        rows = journal.open_lifecycles()
        if not rows:
            print("  (none)")
        for row in rows:
            print(f"  #{row['id']:<5} {row['outcome'] or '?':<8} "
                  f"{row['entry_size']:>9.2f} @ {row['entry_price']:.3f} "
                  f"peak {row['peak_price']:.3f}  {row['question'][:48]}")

        print("\n-- last reconciliation --")
        last = journal.get_state("reconcile.last", None)
        print("  " + (json.dumps(last) if last else "(never run)"))

        cycles = journal.query(
            "SELECT COUNT(*) n, COALESCE(SUM(errors),0) e FROM cycles")
        decisions = journal.query(
            "SELECT action, COUNT(*) n FROM decisions GROUP BY action "
            "ORDER BY n DESC")
        print(f"\n-- activity --\n  cycles: {cycles[0]['n']} "
              f"(errors {cycles[0]['e']})")
        for row in decisions:
            print(f"  {row['action']:<12}: {row['n']}")
        if cfg.intel_path.exists():
            from .analytics.store import IntelStore
            store = IntelStore(cfg.intel_path)
            try:
                stats = store.stats()
                print("\n-- analytical layer --")
                print(f"  wallets observed : {stats.get('wallets', 0):,}")
                print(f"  wallets ranked   : {stats.get('ranked', 0):,}")
                print(f"  trades ingested  : {stats.get('trades', 0):,}")
                print(f"  settled outcomes : {stats.get('resolutions', 0):,}")
                print(f"  anomalies found  : {stats.get('anomalies', 0):,}")
                print(f"  research rows    : {stats.get('research_rows', 0):,} "
                      f"across {stats.get('research_tokens', 0)} token(s)")
            finally:
                store.close()

        print(f"\n-- trading gate --")
        print(f"  kill switch : "
              f"{'ARMED' if cfg.kill_switch_path.exists() else 'off'}")
        if cfg.halt_path.exists():
            try:
                detail = json.loads(cfg.halt_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                detail = {}
            print(f"  halt        : ENGAGED — {detail.get('summary', 'unknown')}")
            print("                clear with: python -m pqb.cli resume --force")
        else:
            print("  halt        : off")
        return 0
    finally:
        journal.close()


def _intel_store(cfg):
    from .analytics.store import IntelStore
    if not cfg.intel_path.exists():
        print(f"No intel store yet at {cfg.intel_path}.")
        print("It is created on the first run — `python -m pqb.cli run`.")
        return None
    return IntelStore(cfg.intel_path)


def cmd_wallets(args) -> int:
    """The derived ranking. Nothing here comes from the config."""
    cfg = _load(args)
    store = _intel_store(cfg)
    if store is None:
        return 1
    try:
        stats = store.stats()
        scores = store.load_scores()
        print(f"observed wallets : {stats.get('wallets', 0):,}")
        print(f"observed trades  : {stats.get('trades', 0):,}")
        print(f"settled outcomes : {stats.get('resolutions', 0):,}  "
              "(what skill is scored against)")
        ranked = sorted((w for w in scores.values() if w.rank),
                        key=lambda w: w.rank)
        if not ranked:
            print("\nNo wallet has enough scored trades to be ranked yet.")
            print("Ranking needs settled markets, so it fills in as markets "
                  "resolve.")
            return 0

        print(f"\n-- ranking ({len(ranked)} ranked of {len(scores)} observed) --")
        header = (f"  {'#':>3} {'wallet':<16} {'score':>6} {'conf':>5} "
                  f"{'n':>4} {'win%':>5} {'avg ret':>8} {'trades':>6} "
                  f"{'mkts':>5}  flags")
        print(header)
        for wallet in ranked[:args.limit]:
            flags = []
            if wallet.in_cohort:
                flags.append("cohort")
            if wallet.pinned:
                flags.append("pinned")
            print(f"  {wallet.rank:>3} {wallet.label:<16} {wallet.score:>6.3f} "
                  f"{wallet.confidence:>5.2f} {wallet.sample:>4} "
                  f"{wallet.win_rate * 100:>5.1f} {wallet.avg_return:>+8.3f} "
                  f"{wallet.trades:>6} {wallet.markets:>5}  "
                  f"{','.join(flags)}")

        unranked = [w for w in scores.values() if not w.rank]
        if unranked:
            print(f"\n  {len(unranked)} observed wallet(s) not yet ranked — "
                  "still tracked, still fed to the engine, simply not enough "
                  "settled trades to order.")
        return 0
    finally:
        store.close()


def cmd_anomalies(args) -> int:
    """What the detectors found, with the numbers behind each one."""
    cfg = _load(args)
    store = _intel_store(cfg)
    if store is None:
        return 1
    try:
        rows = store.recent_anomalies(limit=args.limit, kind=args.kind or "")
        if not rows:
            print("No anomalies recorded yet"
                  + (f" for kind '{args.kind}'." if args.kind else "."))
            return 0

        counts = store.query(
            "SELECT kind, COUNT(*) n, AVG(strength) s FROM anomalies "
            "GROUP BY kind ORDER BY n DESC")
        print("-- detections by kind (all time) --")
        for row in counts:
            print(f"  {row['kind']:<18} {row['n']:>6}   mean strength "
                  f"{row['s']:.3f}")

        print(f"\n-- most recent {len(rows)} --")
        for row in rows:
            when = time.strftime("%Y-%m-%d %H:%M:%S",
                                 time.localtime(float(row["ts"])))
            subject = row["label"] or row["subject"] or ""
            print(f"\n  {when}  {row['kind']}  {subject[:28]}  "
                  f"z={row['z']:.2f} strength={row['strength']:.3f}")
            if row["token_id"]:
                print(f"    token   : {row['token_id'][:24]}... "
                      f"{row['outcome'] or ''}")
            try:
                detail = json.loads(row["detail"] or "{}")
            except json.JSONDecodeError:
                detail = {}
            for key, value in list(detail.items())[:8]:
                print(f"    {key:<22}: {value}")
        return 0
    finally:
        store.close()


def cmd_research(args) -> int:
    """Run strategy discovery over the captured feature series."""
    cfg = _load(args)
    from . import research
    from .analytics.store import IntelStore
    from .quant import available

    ok, why = available()
    if not ok:
        print(f"Quant Bridge unavailable:\n  {why}")
        return 2

    store = IntelStore(cfg.intel_path)
    try:
        result = research.run(
            cfg, store, log=print,
            min_rows=args.min_rows or cfg.research.min_rows,
            max_tokens=args.max_tokens or cfg.research.max_tokens,
            min_tokens=args.min_tokens or cfg.research.min_tokens)
    finally:
        store.close()

    # Persist the funnel exactly as the runner's own passes do. Without this
    # a `research` run from the command line left `discovery.json` holding
    # whatever the last BACKGROUND pass wrote, so `funnel` and the dashboard
    # reported supply and allocation figures from a different run than the
    # library figures beside them — two halves of one screen disagreeing,
    # with nothing on it saying so.
    import json as _json

    with contextlib.suppress(OSError):
        (cfg.data_dir / "discovery.json").write_text(_json.dumps({
            "lastRun": time.time(),
            "source": "cli",
            "candidates": result.candidates,
            "accepted": result.accepted,
            "skippedReason": result.skipped_reason,
            "funnel": result.funnel,
        }), encoding="utf-8")

    print()
    if result.skipped_reason:
        print(f"Research did not run: {result.skipped_reason}")
        return 1
    print(f"tokens exported   : {result.tokens_exported}")
    print(f"tokens researched : {result.tokens_researched}")
    print(f"ranked candidates : {result.candidates}")
    print(f"rules kept        : {result.accepted}")
    for strategy in result.strategies[:10]:
        print(f"\n  [{strategy.accepted_on} tokens] {strategy.describe}")
        print(f"      score {strategy.score:.3f}  sharpe {strategy.sharpe:.2f}"
              f"  oos {strategy.oos_sharpe:.2f}  win {strategy.win_rate*100:.1f}%")
    for error in result.errors:
        print(f"  ! {error}")
    if not result.strategies:
        print("\nNothing survived the cross-token check. That is a result, not "
              f"a failure - across {result.tokens_researched} independent "
              "series, no rule was independently accepted on at least 2 of "
              "them. More history widens the search: "
              "backfill --markets 300 --trades 2000, then research again.")
    return 0


def cmd_strategy(args) -> int:
    """One candidate's full audit trail: which markets, when, with what."""
    cfg = _load(args)
    from .library import (StrategyLibrary, blockers_of, evidence_score,
                          maturity_of)

    path = cfg.data_dir / "library.sqlite3"
    if not path.exists():
        print("No library yet - discovery has not run.")
        return 1
    library = StrategyLibrary(path)
    try:
        needle = str(args.id).lower()
        matches = [s for s in library.all_strategies()
                   if needle in s["id"].lower()]
        if not matches:
            print(f"No strategy matches '{args.id}'.")
            return 1
        if len(matches) > 1:
            print(f"{len(matches)} matches - be more specific:")
            for s in matches[:15]:
                print(f"  {s['id']}")
            return 1
        strategy = matches[0]
        cumulative = library.cumulative(strategy["id"])
        print(f"id        : {strategy['id']}")
        print(f"family    : {strategy.get('family') or '-'}")
        print(f"status    : {strategy['status']}"
              + (f"  ({strategy.get('retired_reason')})"
                 if strategy.get("retired_reason") else ""))
        print(f"describe  : {strategy.get('describe') or '-'}")
        print(f"maturity  : {maturity_of(strategy['status'], cumulative, cfg.research)}")
        print(f"evidence  : {evidence_score(cumulative, cfg.research):.4f}")
        print(f"cumulative: {cumulative['trades']} trades / "
              f"{cumulative['markets']} independent markets / "
              f"expectancy {cumulative['expectancy']:+.4f} / "
              f"top-market share {cumulative['top_share']:.0%}")
        blockers = blockers_of(strategy["status"], cumulative, cfg.research)
        if blockers:
            print("blockers  : " + "; ".join(blockers))
        ledger = library.market_ledger(strategy["id"])
        print(f"\nmarket ledger ({len(ledger)} markets, earned in order):")
        import time as _time
        running_trades = 0
        for row in ledger:
            running_trades += int(row["trades"])
            stamp = _time.strftime("%Y-%m-%d %H:%M",
                                   _time.localtime(float(row["ts"])))
            print(f"  {stamp}  {str(row['market_id'])[:34]:<34} "
                  f"trades {int(row['trades']):>3}  wins {int(row['wins']):>3}"
                  f"  pnl {float(row['pnl']):+8.4f}"
                  f"  (running trades: {running_trades})")
        if not ledger:
            print("  (no OOS evidence yet)")
    finally:
        library.close()
    return 0


def cmd_families(args) -> int:
    """Hypothesis families: evidence across versions, market-deduplicated."""
    cfg = _load(args)
    from .library import StrategyLibrary

    path = cfg.data_dir / "library.sqlite3"
    if not path.exists():
        print("No library yet - discovery has not run.")
        return 1
    library = StrategyLibrary(path)
    try:
        families = library.family_metrics()
    finally:
        library.close()
    print(f"{len(families)} hypothesis families on record\n")
    for family in families[:30]:
        statuses = ",".join(sorted(family.get("statuses") or []))
        print(f"{family['signature'][:52]}")
        print(f"  versions {family['versions']}  "
              f"family evidence: {family['trades']} trades / "
              f"{family['markets']} independent markets  "
              f"expectancy {family['expectancy']:+.4f}  "
              f"top-market share {family['top_share']:.0%}")
        print(f"  statuses: {statuses or '-'}   "
              f"best {str(family.get('bestVersion') or '-')[:40]}")
    print("\nFamily evidence is research interpretation only: versions stay "
          "the atomic records, and trading eligibility runs through the "
          "same per-version validation gates as always.")
    return 0


def cmd_motifs(args) -> int:
    """Recurring STRUCTURE across strategy families, with provenance.

    Reads the library directly rather than the pass's JSON, so the command
    works between passes and reports what the evidence says right now. It
    writes nothing: mining is arithmetic over frozen validation rows.
    """
    cfg = _load(args)
    from . import motif as motif_mod
    from .library import StrategyLibrary
    from .motif import (MotifStore, counterfactual_questions, mine,
                        score_all, search_scale)

    path = cfg.data_dir / "library.sqlite3"
    if not path.exists():
        print("No library yet - discovery has not run.")
        return 1
    library = StrategyLibrary(path)
    try:
        rows = library.all_strategies()
        ledgers = library.evidence_ledgers()
        versions = library.version_counts()
        cumulative = {r["id"]: library.cumulative(r["id"]) for r in rows}
    finally:
        library.close()

    records = mine(rows, ledgers, cumulative=cumulative, versions=versions)
    scores = score_all(records)
    scale = search_scale(records, scores)

    print(f"{len(records)} structural motif(s) examined over {len(rows)} "
          f"library rows\n")
    ranked = sorted(((k, records[k], scores[k]) for k in records
                     if records[k].has_standing),
                    key=lambda item: -item[2].score)
    if not ranked:
        print(f"No motif has standing yet. A motif needs "
              f"{motif_mod.MIN_CANDIDATES} candidates, "
              f"{motif_mod.MIN_INDEPENDENT_MARKETS} independent markets and "
              f"{motif_mod.MIN_INDEPENDENT_CANDIDATES} independent "
              "confirmations before it is allowed an opinion.")
    for key, record, scored in ranked[:int(getattr(args, "limit", 20))]:
        independent = record.independent_candidates()
        print(f"{key[:70]}")
        print(f"  score {scored.score:.3f}  priority weight "
              f"x{scored.weight:.2f}  replication "
              f"{record.replication_rate:.0%}")
        print(f"  {len(record.candidates)} candidate(s) -> "
              f"{len(independent)} INDEPENDENT confirmation(s) across "
              f"{len(record.markets)} non-overlapping market(s), "
              f"{len(record.categories)} categor(ies), "
              f"{len(record.eras)} period(s)")
        print(f"  pooled expectancy {record.expectancy:+.4f} over "
              f"{record.trades} trades  top-market share "
              f"{record.top_market_share:.0%}  attacked {record.attacked} "
              f"(survived {record.adversarial_survival:.0%})")
        if scored.failure_motif:
            print(f"  FAILURE MOTIF: {scored.failure_motif}")
        if scored.why_elevated:
            print(f"  why: {scored.why_elevated[:150]}")
        if scored.why_deprioritised:
            print(f"  why not: {scored.why_deprioritised[:150]}")
        if getattr(args, "questions", False):
            for question in counterfactual_questions(record):
                print(f"    ? {question}")
        print()

    failures = [(k, s) for k, s in scores.items() if s.failure_motif]
    if failures:
        print(f"{len(failures)} recurring FAILURE motif(s) — these throttle "
              "research priority, they never ban a structure:")
        kinds: dict[str, int] = {}
        for _key, scored in failures:
            kinds[scored.failure_motif] = kinds.get(scored.failure_motif, 0) + 1
        for kind, count in sorted(kinds.items(), key=lambda kv: -kv[1]):
            print(f"  {count:>3} x {kind}")
        print()

    print("SEARCH SCALE (multiple testing):")
    for key, value in scale.to_dict().items():
        print(f"  {key}: {value}")
    store_path = cfg.data_dir / "motifs.sqlite3"
    if store_path.exists():
        store = MotifStore(store_path)
        try:
            print("  cumulative across all passes: "
                  + json.dumps(store.cumulative_scale()))
        finally:
            store.close()

    print("\nFamily/motif evidence is RESEARCH evidence. It decides what is "
          "looked at next and nothing else: it cannot promote a candidate, "
          "cannot add to any candidate's out-of-sample record, and cannot "
          "authorise a trade. Every candidate — including one from the "
          "strongest motif here — earns its own evidence on markets it has "
          "never touched, through the same gates as everything else.")
    return 0


def cmd_resize_library(args) -> int:
    """Void validation evidence recorded at the pre-fix account size.

    Run this ONCE, after the sizing fix and before the next research pass.
    See `StrategyLibrary.reset_for_resizing` for why the rows are cleared
    rather than rescaled.
    """
    import shutil
    import time as _time

    cfg = _load(args)
    from .library import SIZING_EPOCH_KEY, StrategyLibrary

    path = cfg.data_dir / "library.sqlite3"
    if not path.exists():
        print("No library yet - nothing to reset.")
        return 1

    library = StrategyLibrary(path)
    try:
        epoch = library.sizing_epoch()
        summary = library.evidence_summary()
    finally:
        library.close()

    if epoch > 0 and not args.force:
        stamped = _time.strftime("%Y-%m-%d %H:%M:%S UTC",
                                 _time.gmtime(epoch))
        print(f"Already reset for sizing on {stamped}.")
        print("Evidence recorded since then is correctly sized and would be "
              "destroyed by running this again.")
        print("Use --force only if the account size has changed again.")
        return 1

    print(f"Library : {path}")
    print(f"  {summary['validations']} validation row(s) across "
          f"{summary['trades']} trades, {summary['pnl']:+,.2f} cumulative P&L")
    print(f"  {summary['passes']} pass record(s)")
    print("  statuses: " + ", ".join(
        f"{k} {v}" for k, v in sorted(summary["statuses"].items())) or "  none")
    print()
    print("This clears every validation and pass row and returns every "
          "strategy to 'new'.")
    print("Rules, descriptions and discovery exclusions are KEPT - only the "
          "verdicts measured at the wrong account size are voided.")

    if not args.yes:
        print()
        print("Re-run with --yes to proceed (a timestamped backup is taken "
              "automatically).")
        return 1

    backup = path.with_name(
        f"library.pre-resize-{_time.strftime('%Y%m%d-%H%M%S')}.sqlite3")
    shutil.copy2(path, backup)
    print(f"\nBackup  : {backup}")

    library = StrategyLibrary(path)
    try:
        result = library.reset_for_resizing(note=args.note or "")
    finally:
        library.close()

    after = result["after"]
    print(f"Cleared : {result['before']['validations']} validation row(s), "
          f"{result['before']['passes']} pass record(s)")
    print(f"Statuses: " + ", ".join(
        f"{k} {v}" for k, v in sorted(after["statuses"].items())))
    print(f"Epoch   : {SIZING_EPOCH_KEY} = "
          f"{_time.strftime('%Y-%m-%d %H:%M:%S UTC', _time.gmtime(result['epoch']))}")
    print()
    print("Next: run `pqb research` to re-earn the evidence against the "
          "frozen holdout pool at the correct account size.")
    return 0


def cmd_walletstates(args) -> int:
    """Wallet behavioral states (the RN1 model): re-derived from our tapes."""
    cfg = _load(args)
    from .analytics.store import IntelStore
    from .analytics.wallet_states import study

    store = IntelStore(cfg.intel_path)
    try:
        targets = [w.address for w in cfg.wallets if w.address]
        targets += [str(a) for a in cfg.research.wallet_state_targets if a]
        ranked = store.query("SELECT wallet FROM wallet_scores "
                             "WHERE rank > 0 ORDER BY rank LIMIT 3")
        targets += [str(r["wallet"]) for r in ranked]
        targets = list(dict.fromkeys(t.lower() for t in targets))[:5]
        result = study(store, targets,
                       cost=cfg.research.assumed_spread
                       + float(cfg.engine.portfolio.fee_per_trade_usdc),
                       premium=cfg.research.wallet_state_max_premium,
                       min_markets=cfg.research.wallet_state_min_markets)
    finally:
        store.close()
    funnel = result["funnel"]
    print(f"target wallets  : {len(funnel.get('wallets', []))}")
    print(f"settled episodes: {funnel.get('episodes', 0)}")
    for wallet, report in (result.get("perWallet") or {}).items():
        episodes = report.get("episodes", 0)
        print(f"\n{wallet[:12]}… : {episodes} episodes", end="")
        if not episodes:
            print("  (no settled first-buys on record yet)")
            continue
        print(f"  baseline win {report['baselineWinRate']*100:.0f}% "
              f"net {report['baselineNet']:+.3f}/share  "
              f"({report['eventuallyTwoSided']} eventually two-sided)")
        for label, cell in (report.get("cells") or {}).items():
            print(f"  {label:<22} {cell['markets']:>4} mkts  "
                  f"win {cell['winRate']*100:.0f}%  net {cell['net']:+.3f}")
    for reason, count in (funnel.get("rejectReasons") or {}).items():
        print(f"  rejected: {count} x {reason}")
    print(f"\ncandidates kept: {funnel.get('kept', 0)} - research only; "
          "checkpoint-frozen features, no future information, and nothing "
          "trades without the full OOS ladder.")
    return 0


def _hold_text(seconds: float) -> str:
    """Holding periods span seconds to weeks; one unit cannot show that."""
    seconds = float(seconds or 0.0)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds/60.0:.0f}m"
    if seconds < 172800:
        return f"{seconds/3600.0:.1f}h"
    return f"{seconds/86400.0:.1f}d"


def cmd_walletbehavior(args) -> int:
    """Wallet behavioral discovery: the repeating behavior behind ranked
    wallets, extracted as wallet-free hypotheses. Research only."""
    cfg = _load(args)
    from .analytics import wallet_behavior as wb
    from .analytics.store import IntelStore

    store = IntelStore(cfg.intel_path)
    try:
        pinned = [w.address for w in cfg.wallets if w.address]
        pinned += [str(a) for a in cfg.research.wallet_state_targets if a]
        result = wb.study(
            store, pinned=pinned,
            cost=cfg.research.assumed_spread
            + float(cfg.engine.portfolio.fee_per_trade_usdc),
            min_trades=cfg.research.wallet_behavior_min_trades,
            min_markets=cfg.research.wallet_behavior_min_markets,
            min_repeat=cfg.research.wallet_behavior_min_repeat,
            min_wallet_trades=cfg.research.wallet_behavior_min_wallet_trades,
            max_wallets=cfg.research.wallet_behavior_max_wallets,
            top_n=cfg.research.wallet_behavior_register_top)
    finally:
        store.close()
    funnel = result["funnel"]
    print(f"wallets considered : {funnel.get('walletsConsidered', 0)} "
          f"(eligible: {funnel.get('walletsEligible', 0)})")
    print(f"observations       : {funnel.get('observations', 0)} "
          f"({funnel.get('settledObservations', 0)} settled)")
    print(f"behavior cells     : {funnel.get('cellsFormed', 0)}   "
          f"move threshold: {funnel.get('moveThreshold', 0)}")
    for wallet, report in (result.get("perWallet") or {}).items():
        n = report.get("observations", 0)
        print(f"\n{wallet[:12]}… : {n} engagement(s)", end="")
        if not report.get("settled"):
            print("  (nothing settled on record yet)")
            continue
        holds = ", ".join(f"{v} {k}" for k, v in
                          (report.get("holdClasses") or {}).items())
        print(f"  settled {report['settled']} across "
              f"{report['markets']} INDEPENDENT market(s), win "
              f"{report['winRate']*100:.0f}%, avg {report['avgReturn']:+.3f}"
              f"/share, {report['switchedSides']} switched sides, "
              f"avg adds {report['avgAdds']}")
        if holds:
            print(f"  holding behavior: {holds}", end="")
            median = report.get("medianHold")
            if median:
                print(f" | median {_hold_text(median)}", end="")
                win_hold, lose_hold = (report.get("winningHold"),
                                       report.get("losingHold"))
                if win_hold and lose_hold:
                    print(f" (winners {_hold_text(win_hold)} vs losers "
                          f"{_hold_text(lose_hold)})", end="")
            print()
        print(f"  entry-price bias: {report.get('entryPriceBias') or '-'} "
              f"| sequence: {report.get('sequencePattern') or '-'}")
        print(f"  sample quality: {report.get('sampleQuality', 0):.2f} "
              f"| research priority: "
              f"{report.get('researchPriority', 0):.2f}")
        two = report.get("twoSided") or {}
        if two.get("verdict"):
            kinds = two.get("kinds") or {}
            print(f"  two-sided: {kinds.get('simultaneous_two_sided', 0)} "
                  f"hedge-like / {kinds.get('sequential_two_sided', 0)} "
                  f"reversal-like / {kinds.get('one_sided', 0)} one-sided")
            print(f"  -> {two['verdict']}")
        if report.get("switches"):
            print(f"  side switches: {report['switches']} "
                  f"({report.get('switchRate', 0)*100:.0f}% of entries, "
                  f"{report.get('switchAfterLosing', 0)} after losing / "
                  f"{report.get('switchAfterWinning', 0)} after winning, "
                  f"across {report.get('switchMarkets', 0)} market(s)) - "
                  f"post-switch win "
                  f"{report.get('postSwitchWinRate', 0)*100:.0f}%, "
                  f"avg {report.get('postSwitchAvgReturn', 0):+.3f}/share")
    for reason, count in (funnel.get("rejectReasons") or {}).items():
        print(f"  rejected: {count} x {reason}")
    print(f"\nhypotheses kept: {funnel.get('kept', 0)} "
          f"({funnel.get('multiWallet', 0)} supported by 2+ wallets, "
          f"{funnel.get('keptSwitch', 0)} from conditional side-switching, "
          f"{funnel.get('duplicatesMerged', 0)} duplicates merged)")
    for rule in result.get("candidates") or []:
        print(f"  {wb.describe(rule)}")
        print(f"    source: {rule['source_trades']} trades / "
              f"{rule['source_markets']} markets / "
              f"{rule['supporting_wallets']} wallet(s), "
              f"win {rule['source_win_rate']*100:.0f}%, "
              f"net {rule['source_net']:+.3f}/share")
    pooled = funnel.get("twoSided") or {}
    if pooled.get("verdict"):
        print("\nTWO-SIDED HYPOTHESIS (pooled across eligible wallets)")
        print(f"  {pooled['verdict']}")
        for band, cell in (pooled.get("byEntryPrice") or {}).items():
            print(f"  {band:<10} one-sided {cell['oneSided']['expectancy']:+.3f}"
                  f" ({cell['oneSided']['markets']} mkts)  vs  two-sided "
                  f"{cell['twoSided']['expectancy']:+.3f} "
                  f"({cell['twoSided']['markets']} mkts)  -> "
                  f"{cell['incremental']:+.3f}")
    print("\nresearch only: the wallet inspires the hypothesis, replay on "
          "markets the wallet never traded is the judge, and nothing "
          "trades without the full OOS ladder.")
    return 0


def cmd_longshot(args) -> int:
    """The military-attack longshot calibration: implied vs realized."""
    cfg = _load(args)
    from .analytics.longshot import study
    from .analytics.store import IntelStore

    store = IntelStore(cfg.intel_path)
    try:
        result = study(store,
                       cost=cfg.research.assumed_spread
                       + float(cfg.engine.portfolio.fee_per_trade_usdc),
                       min_events=cfg.research.longshot_min_events)
    finally:
        store.close()
    funnel = result["funnel"]
    print(f"settled tapes      : {funnel['settledTapes']}")
    print(f"observations       : {funnel['observations']} (entry-time only)")
    print(f"military markets   : {funnel['militaryMarkets']}   "
          f"control markets: {funnel['controlMarkets']}")
    for title, key in (("military calibration", "calibrationMilitary"),
                       ("control calibration", "calibrationControl")):
        cells = funnel.get(key) or {}
        if not cells:
            print(f"\n{title}: no cells yet")
            continue
        print(f"\n{title} (implied -> realized, per probability band):")
        for band, cell in cells.items():
            print(f"  {band}: implied {cell['implied']:.2f} -> realized "
                  f"{cell['realized']:.2f} (edge {cell['edge']:+.2f}, "
                  f"net {cell['netPerShare']:+.3f}/share, "
                  f"{cell['events']} events, {cell['markets']} markets)")
    for reason, count in (funnel.get("rejectReasons") or {}).items():
        print(f"  rejected: {count} x {reason}")
    print(f"\ncandidates kept: {funnel['kept']} - research only; nothing "
          "trades without the full OOS ladder.")
    return 0


def cmd_activity(args) -> int:
    """Backfill non-trade wallet activity: REDEEM, MERGE, SPLIT.

    The trade tape comes from /trades, which carries only trades — which is
    why every event in the store has been a BUY or a SELL, and why the
    lifecycle layer has had to report redemptions as unavailable. /activity is
    the wider feed and carries them.
    """
    import asyncio

    import httpx

    cfg = _load(args)
    from .analytics.activity import collect
    from .analytics.store import IntelStore

    store = IntelStore(cfg.intel_path)
    before = store.activity_census()
    wallets = store.busiest_wallets(limit=int(args.wallets),
                                    min_trades=int(args.min_trades))
    print(f"on record: {before}")
    print(f"collecting non-trade activity for {len(wallets):,} wallet(s) "
          f"with >= {args.min_trades} trades")
    if not wallets:
        print("No wallet meets the floor.")
        store.close()
        return 1
    print()

    async def main():
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=12.0),
                headers={"User-Agent": "PolymarketQuantBridge/0.1"}) as http:
            return await collect(
                http, store, cfg.polymarket.data_api_host, wallets,
                max_pages=int(args.pages), pause_seconds=float(args.pause),
                progress=print)

    try:
        result = asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted — everything collected so far is saved")
        store.close()
        return 1

    after = store.activity_census()
    print()
    print(f"fetched {result.fetched:,} activity row(s) over "
          f"{result.wallets:,} wallet(s)")
    print(f"stored {result.stored:,} new non-trade event(s): {result.by_type}")
    print(f"census: {before} -> {after}")
    if result.errors:
        print(f"errors: {result.errors} — {result.error_samples[:2]}")
    print()
    print("The lifecycle layer now has redemptions: a condition the wallet "
          "has redeemed is FINISHED, whatever the tape's end date says.")
    store.close()
    return 0


def cmd_settlements(args) -> int:
    """Drain the settlement backlog off the trading loop.

    The bot resolves 60 markets per 5 minutes while it runs, which needs about
    a day of uptime to catch up on a 16k-market backlog. Until it does, every
    research result that depends on an outcome — graded predictions, realised
    P&L, wallet scoring — is capped at whatever small number happens to be
    resolved. This is the same sweep, batched, resumable, and off the loop.

    Writes only decisive outcomes, and only to the `resolutions` table.
    """
    import asyncio

    cfg = _load(args)
    from .adapters.data_adapter import PolymarketDataAdapter
    from .logs import Log
    from .analytics.settlements import drain
    from .analytics.store import IntelStore

    store = IntelStore(cfg.intel_path)
    before = store.stats()
    pending = len(store.markets_without_resolution(limit=100_000))
    print(f"{before.get('resolutions', 0):,} outcome(s) already known; "
          f"{pending:,} traded market(s) still unresolved.")
    if not pending:
        print("Nothing to do.")
        store.close()
        return 0
    print("Asking Gamma how they ended. Only DECISIVE settlements are "
          "recorded — a market that closed without resolving is left alone.")
    print()

    async def main():
        adapter = PolymarketDataAdapter(cfg, Log())
        try:
            await adapter.start()
            return await drain(store, adapter.settlements,
                               batch=int(args.batch),
                               max_batches=int(args.max_batches),
                               pause_seconds=float(args.pause),
                               patience=int(args.patience),
                               progress=print)
        finally:
            with contextlib.suppress(Exception):
                await adapter.stop()

    try:
        result = asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted — everything resolved so far is already saved")
        store.close()
        return 1

    after = store.stats()
    print()
    print(f"checked {result.checked:,} market(s) in {result.batches} batch(es)")
    print(f"newly settled: {result.settled_markets:,} market(s), "
          f"{result.settled_tokens:,} outcome(s)")
    print(f"resolutions on record: {before.get('resolutions', 0):,} -> "
          f"{after.get('resolutions', 0):,}")
    if result.errors:
        print(f"errors: {result.errors} — {result.error_samples[:2]}")
    if not result.exhausted:
        print("Backlog NOT exhausted. Run again to continue; it resumes "
              "where it stopped.")
    print()
    print("Re-run `pqb wallet-state-research` to see how many pending "
          "predictions this resolved.")
    store.close()
    return 0


def cmd_wallet_state_research(args) -> int:
    """The Wallet State Transition Research module — read-only, isolated.

    Runs regardless of `wallet_state_research.enabled`, because that flag
    guards whether the module may reach the ENGINE, not whether an operator
    may run the study by hand. Nothing this command does can alter a strategy,
    a signal, a position or a risk control: it opens the intel store read-only
    and writes reports.
    """
    cfg = _load(args)
    from .wallet_state_research.runner import RunConfig, run
    from .wallet_state_research.report import render, summary

    settings = cfg.wallet_state_research
    out_dir = (Path(args.out) if getattr(args, "out", "")
               else cfg.data_dir / "research" / "wallet_state")
    run_config = RunConfig(
        intel_path=str(cfg.intel_path),
        out_dir=str(out_dir),
        horizon_minutes=float(getattr(args, "horizon", 0)
                              or settings.signal_horizon_minutes),
        extra_horizons=tuple(settings.extra_horizons),
        frozen_rn1_enabled=settings.frozen_rn1_enabled,
        cross_wallet_enabled=(settings.cross_wallet_enabled
                              and not getattr(args, "rn1_only", False)),
        cross_market_enabled=(settings.cross_market_enabled
                              and not getattr(args, "rn1_only", False)),
        discovery_enabled=(settings.discovery_enabled
                           or getattr(args, "discovery", False)),
        minimum_wallet_samples=settings.minimum_wallet_samples,
        minimum_market_samples=settings.minimum_market_samples,
        stakes=tuple(settings.stakes),
        quiet_days=settings.label_quiet_days,
        max_wallets=int(getattr(args, "max_wallets", 0)
                        or settings.max_wallets),
        min_wallet_trades=settings.min_wallet_trades,
        walk_forward_folds=settings.walk_forward_folds,
        frozen_v1_enabled=settings.frozen_v1_enabled,
        prediction_freshness_minutes=settings.prediction_freshness_minutes,
        transitions_enabled=settings.transitions_enabled,
        structure_discovery_enabled=(settings.structure_discovery_enabled
                                     or getattr(args, "structure", False)))

    result = run(run_config, say=(None if getattr(args, "quiet", False)
                                  else print))
    if getattr(args, "json", False):
        print(json.dumps(summary(result), indent=2, default=str))
    else:
        print()
        print(render(result))
    print()
    print(f"reports written to {out_dir}")
    return 0 if result.get("available") else 1


def cmd_forensics(args) -> int:
    """Why the account is losing: attribution, counterfactuals, hypotheses.

    Read-only. Nothing this command prints has been applied, and nothing it
    prints can be applied by running it again — every proposal is a new
    risk-policy version that has to be tested on data it was not derived from.
    """
    cfg = _load(args)
    from .analytics import forensics

    data = forensics.report(
        cfg.data_dir / "journal.sqlite3",
        cfg.intel_path if getattr(cfg, "intel_path", None) else "",
        starting_balance=float(cfg.mode.paper_starting_balance or 0.0))
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2, default=str))
        return 0 if data.get("available") else 1
    print(forensics.daily_report(data))
    if getattr(args, "write", False) and data.get("available"):
        out = cfg.data_dir / "forensics.json"
        out.write_text(json.dumps(data, indent=2, default=str),
                       encoding="utf-8")
        print(f"\nwritten: {out}")
    return 0 if data.get("available") else 1


def cmd_attribution(args) -> int:
    """Where profit is gained and leaked - evidence, not speculation."""
    cfg = _load(args)
    from .analytics.attribution import report

    data = report(cfg.data_dir / "journal.sqlite3")
    if not data.get("available"):
        print(f"No attribution yet: {data.get('reason', 'no journal')}")
        return 1
    net = data.get("net", {})
    print(f"closed trades : {data.get('closedTrades', 0)}")
    print(f"net P&L       : {net.get('netPnl', 0):+.4f} USDC "
          f"(win rate {net.get('winRate', 0)*100:.0f}%, "
          f"expectancy {net.get('expectancy', 0):+.4f}/trade)")
    execution = data.get("execution", {})
    print(f"execution     : {execution.get('fills', 0)} fills, "
          f"{execution.get('unfilled', 0)} unfilled, "
          f"fees {execution.get('feesPaid', 0):.4f}, "
          f"slippage {execution.get('slippagePaid', 0):.4f}")
    if data.get("largestLeak"):
        print(f"largest leak  : {data['largestLeak']}")
    for title, key in (("by price bucket", "byPriceBucket"),
                       ("by holding time", "byHoldTime"),
                       ("by time-to-resolution", "byTimeToResolution"),
                       ("by exit reason", "byExitReason")):
        buckets = data.get(key) or {}
        if not buckets:
            continue
        print(f"\n{title}:")
        for name, b in sorted(buckets.items(),
                              key=lambda kv: -kv[1]["trades"]):
            print(f"  {name:<16} {b['trades']:>4} trades  "
                  f"net {b['netPnl']:+9.4f}  "
                  f"expect {b['expectancy']:+.4f}  win {b['winRate']*100:.0f}%")
    skips = data.get("skipReasons") or {}
    if skips:
        print("\nwhy the bot said no (top reasons, numbers collapsed to N):")
        for reason, n in list(skips.items())[:8]:
            print(f"  {n:>6}  {reason}")
    print("\nRead-only research: nothing here gates a trade. A finding "
          "becomes a production filter only after out-of-sample proof.")
    return 0


def cmd_cascade(args) -> int:
    """The liquidation-cascade event study: measured, never assumed."""
    cfg = _load(args)
    from .analytics.cascade import CascadeStore, analyse

    path = cfg.data_dir / "cascade.sqlite3"
    if not path.exists():
        print("No cascade capture yet - the monitor records liquidation "
              "events while the bot runs (cascade.enabled in config).")
        return 1
    store = CascadeStore(path)
    try:
        data = analyse(store, min_sample=cfg.cascade.min_sample)
    finally:
        store.close()
    print(f"events captured   : {data['events']} "
          f"({data['qualifying']} above the ${cfg.cascade.qualify_liquidation_usd:,.0f} "
          "starting-point flag)")
    print(f"with outcome      : {data['withOutcome']}   "
          f"baseline windows: {data['baselineWindows']}")
    for side in ("long", "short"):
        liq = data.get(f"{side}_liq", {})
        tag = "" if liq.get("sufficient") else "  [sample insufficient]"
        print(f"{side:<5} liq -> {liq.get('predicted', '?'):<4}: "
              f"{liq.get('events', 0):>4} events, "
              f"hit {liq.get('hitRate', 0)*100:.0f}%, "
              f"net {liq.get('netPerTrade', 0):+.4f}/trade{tag}")
    print("\nBTC response after events (vs baseline drift "
          f"{data.get('baselineAbsMove60sPct', 0):.4f}% abs @60s):")
    for horizon, row in (data.get("responseCurve") or {}).items():
        print(f"  {horizon:>3}s  n={row['n']:<4} "
              f"abs {row['meanAbsMovePct']:+.4f}%  "
              f"predicted-way {row['meanPredictedMovePct']:+.4f}%")
    if data.get("bySize"):
        print("\nby liquidation size:")
        for name, row in data["bySize"].items():
            print(f"  {name:<9} {row['events']:>4} events  "
                  f"abs move @60s {row['meanAbsMove60sPct']:.4f}%")
    print(f"\nverdict: {data.get('verdict')} - {data.get('verdictWhy')}")
    print("Research only: nothing here can trade until it passes the same "
          "frozen out-of-sample validation as every other candidate.")
    return 0


def _wallet_positions(store, wallet, paths, resolutions):
    """Entry price, price path after entry, and settlement, per position."""
    import collections
    rows = store.trades_for_wallet(wallet, limit=20_000)
    by_token = collections.defaultdict(lambda: {"cost": 0.0, "shares": 0.0,
                                                "ts": 0, "market": "",
                                                "question": ""})
    for row in rows:
        if str(row.get("side") or "BUY").upper() != "BUY":
            continue
        token = str(row.get("token_id") or "")
        if token not in resolutions:
            continue
        acc = by_token[token]
        acc["cost"] += float(row.get("usdc") or 0.0)
        acc["shares"] += float(row.get("size") or 0.0)
        acc["ts"] = acc["ts"] or int(row.get("ts") or 0)
        acc["market"] = str(row.get("market_id") or "")
        acc["question"] = acc["question"] or str(row.get("question") or "")

    out = []
    for token, acc in by_token.items():
        if acc["shares"] <= 0:
            continue
        entry = acc["cost"] / acc["shares"]
        # Only the path AFTER entry matters; earlier prices were never
        # available to a decision we had not yet made.
        after = [(ts, pr) for ts, pr in paths.get(token, []) if ts >= acc["ts"]]
        out.append({"entry": entry, "path": after,
                    "settled": resolutions[token], "market": acc["market"],
                    "question": acc["question"]})
    return out


def cmd_playbook(args) -> int:
    """The optimal exit rule for every ranked wallet."""
    cfg = _load(args)
    store = _intel_store(cfg)
    if store is None:
        return 1
    try:
        from .analytics.playbook import build, price_paths

        resolutions = store.resolutions()
        if not resolutions:
            print("No settled markets known yet.")
            print("Run:  python -m pqb.cli backfill")
            return 1
        paths = price_paths(store.trades_since(0))

        scores = store.load_scores()
        if args.address:
            targets = [args.address.lower()]
        else:
            ranked = sorted((w for w in scores.values() if w.rank),
                            key=lambda w: w.rank)
            targets = [w.wallet for w in ranked[:args.limit]]
        if not targets:
            print("No ranked wallets yet. Run the bot, then backfill.")
            return 1

        print("=" * 78)
        print("  PER-WALLET PLAYBOOK - the exit rule that would have worked best")
        print("=" * 78)
        print("  Not copying their exits: most of these wallets never sell.")
        print("  This asks what OUR best exit would have been, given where they")
        print("  entered and how the price actually moved afterwards.")
        print()

        books = []
        for wallet in targets:
            book = build(wallet, _wallet_positions(store, wallet, paths,
                                                   resolutions))
            if book.best:
                books.append((scores.get(wallet), book))

        if not books:
            print("  No wallet has enough settled positions with a price path yet.")
            print("  Run `python -m pqb.cli backfill --markets 150` for more history.")
            return 0

        books.sort(key=lambda kv: kv[1].best.score, reverse=True)
        print(f"  {'wallet':<16} {'pos':>4} {'best rule':<28} {'avg':>8} "
              f"{'win%':>6} {'vs hold':>8}")
        for intel, book in books:
            label = (intel.label if intel else book.wallet[:12])
            print(f"  {label:<16} {book.positions:>4} "
                  f"{book.best.rule.describe():<28} "
                  f"{book.best.mean_return:>+7.1%} {book.best.win_rate:>5.0%} "
                  f"{book.improvement:>+7.1%}")

        held = sum(1 for _i, b in books if b.best.rule.hold)
        print()
        print(f"  {len(books)} wallet(s) analysed. For {held} of them nothing beat")
        print("  simply holding to resolution - their own default is already best.")
        print(f"  For the other {len(books) - held}, an exit rule would have improved things.")

        if args.address and books:
            _intel, book = books[0]
            print()
            print("  EVERY RULE TESTED, BEST FIRST")
            print(f"    {'rule':<28} {'n':>4} {'avg':>8} {'win%':>6} "
                  f"{'worst':>8} {'early':>6}")
            for result in book.ranked[:10]:
                print(f"    {result.rule.describe():<28} {result.n:>4} "
                      f"{result.mean_return:>+7.1%} {result.win_rate:>5.0%} "
                      f"{result.worst:>+7.1%} {result.early:>6}")
            print()
            print(f"  {book.note}")
        print()
        return 0
    finally:
        store.close()


def cmd_strategies(args) -> int:
    """Per-market strategy for each top-ranked wallet — not one rule per wallet.

    The premise, which is David's: a ranked wallet is running several algorithms
    at once — one for sports, one for politics, one for crypto — and scoring it
    as a single strategy averages them into mush. This splits every top wallet
    by market category and fits the best exit rule to each segment separately.
    """
    cfg = _load(args)
    store = _intel_store(cfg)
    if store is None:
        return 1
    try:
        from .analytics.playbook import build_segmented, price_paths
        from .analytics import segments

        resolutions = store.resolutions()
        if not resolutions:
            print("No settled markets known yet.")
            print("Run:  python -m pqb.cli backfill --markets 150")
            return 1
        paths = price_paths(store.trades_since(0))

        scores = store.load_scores()
        if args.address:
            targets = [args.address.lower()]
        else:
            ranked = sorted((w for w in scores.values() if w.rank),
                            key=lambda w: w.rank)
            targets = [w.wallet for w in ranked[:args.limit]]
        if not targets:
            print("No ranked wallets yet. Run the bot, then backfill.")
            return 1

        print("=" * 82)
        print("  PER-MARKET STRATEGY BY WALLET")
        print("=" * 82)
        print("  Each wallet is treated as SEVERAL algorithms - one per market")
        print("  category - because that is how these operators actually trade.")
        print("  A segment needs at least "
              f"{args.min_positions} settled positions before a rule is fitted.")
        print()

        header = (f"  {'wallet':<16} {'market':<14} {'pos':>4} "
                  f"{'best rule':<30} {'avg':>8} {'win%':>6} {'vs hold':>8}")
        any_rows = False
        # Rank each wallet's segments so the strongest, most-populated strategy
        # for that wallet is shown first.
        rows_for_ranking = []
        for wallet in targets:
            positions = _wallet_positions(store, wallet, paths, resolutions)
            books = build_segmented(wallet, positions,
                                    min_positions=args.min_positions)
            intel = scores.get(wallet)
            label = intel.label if intel else wallet[:12]
            for category, book in books.items():
                if not book.best:
                    continue
                rows_for_ranking.append((label, category, book))

        if not rows_for_ranking:
            print("  No wallet has a segment with enough settled positions yet.")
            print("  Widen history:  python -m pqb.cli backfill --markets 300")
            return 0

        rows_for_ranking.sort(key=lambda r: (r[0], -r[2].best.score))
        print(header)
        last_label = None
        for label, category, book in rows_for_ranking:
            shown_label = label if label != last_label else ""
            last_label = label
            print(f"  {shown_label:<16} {category:<14} {book.positions:>4} "
                  f"{book.best.rule.describe():<30} "
                  f"{book.best.mean_return:>+7.1%} {book.best.win_rate:>5.0%} "
                  f"{book.improvement:>+7.1%}")
            any_rows = True

        if any_rows:
            print()
            print("  Read each row as: for THIS wallet in THIS market, the exit")
            print("  rule shown would have beaten holding by the 'vs hold' amount.")
            print("  Rules including 'trail' or 'time-exit' are the spike-capture")
            print("  and impulse-and-out families - they ride a sudden move up and")
            print("  leave before it round-trips.")
        print()
        return 0
    finally:
        store.close()


def cmd_backfill(args) -> int:
    """Pull already-resolved markets so wallets can be studied immediately."""
    import asyncio

    import httpx

    cfg = _load(args)
    from .analytics.backfill import run as backfill_run
    from .analytics.store import IntelStore

    store = IntelStore(cfg.intel_path)
    before = store.stats()
    print("Backfilling settled market history.")
    print("Watching live markets produces almost no resolved outcomes - they")
    print("are live because they have NOT settled. This pulls markets that")
    print("already finished, with their full trade history and price paths.")
    print()

    async def main():
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=12.0),
                headers={"User-Agent": "PolymarketQuantBridge/0.1"}) as http:
            return await backfill_run(
                http, store, gamma_host=cfg.polymarket.gamma_host,
                data_host=cfg.polymarket.data_api_host,
                markets=args.markets, trades_per_market=args.trades,
                progress=print,
                uncertain_lo=cfg.research.uncertain_min_price,
                uncertain_hi=cfg.research.uncertain_max_price,
                max_volume=(cfg.research.backfill_max_volume
                            if args.max_volume is None else args.max_volume),
                min_volume=(cfg.research.backfill_min_volume
                            if args.min_volume is None else args.min_volume))

    try:
        result = asyncio.run(main())
        after = store.stats()
        print()
        print(f"  markets examined  : {result.markets_seen}")
        print(f"  markets used      : {result.markets_used}")
        print(f"  new trades stored : {result.trades_written:,}")
        print(f"  settlements stored: {result.resolutions_written:,}")
        print(f"  wallets seen      : {result.wallets:,}")
        if result.skipped:
            print("  skipped:")
            for why, n in sorted(result.skipped.items(), key=lambda kv: -kv[1]):
                print(f"    {why}: {n}")
        print()
        print(f"  settled outcomes known: {before.get('resolutions', 0):,}"
              f" -> {after.get('resolutions', 0):,}")
        print(f"  wallets observed      : {before.get('wallets', 0):,}"
              f" -> {after.get('wallets', 0):,}")
        print()
        print("  Now run:  python -m pqb.cli wallets")
        print("            python -m pqb.cli playbook")
        return 0
    finally:
        store.close()


def cmd_wallet(args) -> int:
    """Reverse-engineer one wallet's method from its public history."""
    cfg = _load(args)
    store = _intel_store(cfg)
    if store is None:
        return 1
    try:
        from .analytics.reverse import HELD_LOSS, describe, reconstruct

        target = args.address.lower()
        rows = store.trades_for_wallet(target, limit=20_000)
        if not rows:
            print(f"No observed trades for {target}.")
            print("The bot only knows wallets it has seen trade while running.")
            return 1

        positions = reconstruct(rows, store.resolutions())

        if getattr(args, "by_market", False):
            from .analytics import segments

            print("=" * 74)
            print(f"  WALLET {target}  -  BY MARKET")
            print("=" * 74)
            print("  The same wallet, split into the separate strategies it runs")
            print("  in each market category. Ordered by how much it put to work.")
            print()
            groups = segments.split(positions, key=lambda p: p.question)
            ordered = sorted(
                groups.items(),
                key=lambda kv: -sum(p.gross_bought for p in kv[1]))
            for category, group in ordered:
                seg = describe(target, group)
                if not seg.settled and seg.invested < 1:
                    continue
                print(f"  {category.upper()}  ({seg.positions} positions, "
                      f"{seg.settled} finished)")
                print(f"    put in ${seg.invested:,.2f}   "
                      f"made ${seg.realized:,.2f}", end="")
                if seg.settled:
                    print(f"   win rate {seg.win_rate:.0%}", end="")
                if seg.median_entry:
                    print(f"   median entry {seg.median_entry:.2f}", end="")
                print()
                if seg.notes:
                    print(f"    - {seg.notes[0]}")
                print()
            print("  For the optimal exit rule per market, run:")
            print(f"    python -m pqb.cli strategies --address {target}")
            print()
            return 0

        method = describe(target, positions)
        print("=" * 74)
        print(f"  WALLET {target}")
        print("=" * 74)
        print()
        print(f"  positions reconstructed : {method.positions}")
        print(f"  finished                : {method.settled}")
        print(f"  total put in            : ${method.invested:,.2f}")
        print(f"  made / lost             : ${method.realized:,.2f}")
        if method.settled:
            print(f"  win rate                : {method.win_rate:.0%}")

        print()
        print("  HOW ITS POSITIONS ENDED")
        for how, n in sorted(method.counts.items(), key=lambda kv: -kv[1]):
            share = n / method.positions if method.positions else 0
            print(f"    {how:<22} {n:>5}  ({share:.0%})")
        if method.counts.get(HELD_LOSS):
            print()
            print("    ^ THIS is where the losing trades are. A loser held to")
            print("      resolution expires worth $0 and produces NO sell order,")
            print("      so it never appears in a list of settled trades.")

        if method.by_price:
            print()
            print("  WHERE THE MONEY IS MADE, BY ENTRY PRICE")
            print(f"    {'bought at':<12} {'n':>5} {'win%':>6} {'put in':>12} "
                  f"{'made':>12} {'return':>8}")
            for b in method.by_price:
                print(f"    {b['range']:<12} {b['n']:>5} {b['winRate']:>5.0%} "
                      f"${b['invested']:>11,.0f} ${b['realized']:>11,.0f} "
                      f"{b['returnPct']:>+7.1%}")

        print()
        print("  WHAT IT APPEARS TO DO")
        for note in method.notes:
            print(f"    - {note}")
        print()
        return 0
    finally:
        store.close()


def cmd_lifecycle(args) -> int:
    """How a wallet MANAGES a market: entry, adds, and the flip to the other side."""
    cfg = _load(args)
    store = _intel_store(cfg)
    if store is None:
        return 1
    try:
        from .analytics.lifecycle import profile, reconstruct

        target = args.address.lower()
        rows = store.trades_for_wallet(target, limit=20_000)
        if not rows:
            print(f"No observed trades for {target}.")
            print("The bot only knows wallets it has seen trade while running.")
            return 1

        lives = reconstruct(rows, store.resolutions())
        prof = profile(target, lives)

        print("=" * 74)
        print(f"  LIFECYCLE  {target}")
        print("=" * 74)
        print("  Not 'what does it buy' but 'how does it manage the position'.")
        print()
        print(f"  markets touched      : {prof.markets}")
        print(f"  resolved so far      : {prof.resolved}")
        print(f"  median entry price   : {prof.median_entry:.2f}")
        print(f"  adds per market      : {prof.adds_per_market:.1f}")
        print(f"  of adds, averaged DN : {prof.avg_down_rate:.0%}")
        print(f"  flips to other side  : {prof.flip_rate:.0%} of markets"
              f"  ({prof.flips_defending} defending, "
              f"{prof.flips_locking} locking)")
        if prof.median_flip_self:
            moved = ("DOWN" if prof.median_flip_self < prof.median_flip_entry
                     else "UP")
            print(f"  at the flip          : entry outcome went from "
                  f"~{prof.median_flip_entry:.2f} to ~{prof.median_flip_self:.2f} "
                  f"({moved}), then it bought the other side")

        print()
        print("  CASH FLOW - the honest version")
        print(f"    put in            : ${prof.cash_in:,.2f}")
        print(f"    came back         : ${prof.cash_back:,.2f}"
              "   (sells + redeemed winners)")
        print(f"    STILL AT RISK     : ${prof.still_at_risk:,.2f}"
              "   (cost in unresolved positions)")
        print(f"    realized P&L      : ${prof.realized:,.2f}"
              "   (on resolved positions only)")
        if prof.still_at_risk > 0:
            print("    ^ any 'never loses' read ignores the still-at-risk line.")

        if prof.band.get("resolved"):
            b = prof.band
            print()
            print("  THE 60-79c ENTRY BAND (the signal you flagged)")
            print(f"    positions in band : {b['n']}  ({b['resolved']} resolved)")
            print(f"    win rate          : {b['winRate']:.0%}")
            print(f"    return            : {b['returnPct']:+.1%} "
                  f"on ${b['invested']:,.0f}")

        print()
        print("  WHAT ITS POSITION MANAGEMENT LOOKS LIKE")
        for note in prof.notes:
            print(f"    - {note}")
        print()
        return 0
    finally:
        store.close()


def cmd_onchain(args) -> int:
    """True per-wallet P&L: CLOB trades PLUS the on-chain flows the tape hides.

    Acceptance 11.3 and the honest §6: reconstruct a wallet's economics from CLOB
    trades AND the Conditional-Token split/merge/redeem events, and show that the
    CLOB-only view differs — which is why a wallet can look like it never loses.
    """
    import asyncio

    import httpx

    from .chain.ctf import TruePnL
    from .chain.reader import CTFReader

    cfg = _load(args)
    store = _intel_store(cfg)
    if store is None:
        return 1
    try:
        target = args.address.lower()
        rows = store.trades_for_wallet(target, limit=50_000)
        clob_bought = sum(float(r.get("usdc") or 0.0) for r in rows
                          if str(r.get("side") or "BUY").upper() == "BUY")
        clob_sold = sum(float(r.get("usdc") or 0.0) for r in rows
                        if str(r.get("side") or "").upper() == "SELL")

        print("=" * 74)
        print(f"  TRUE P&L  {target}")
        print("=" * 74)
        print(f"  CLOB bought : ${clob_bought:,.2f}")
        print(f"  CLOB sold   : ${clob_sold:,.2f}")
        print(f"  CLOB-only P&L (what the order book alone shows): "
              f"${clob_sold - clob_bought:,.2f}")
        print()

        rpc = cfg.polymarket.polygon_rpc_url.strip()
        if not rpc:
            print("  ON-CHAIN RECONSTRUCTION IS NOT YET ENABLED.")
            print("  It needs a Polygon RPC endpoint, which is the deferred")
            print("  Phase-3 decision. Set PQB_POLYGON_RPC_URL in .env (or")
            print("  polymarket.polygon_rpc_url) and re-run to see the TRUE P&L")
            print("  including merges and redemptions - the invisible exits.")
            print()
            print("  Until then only the CLOB-only figure above is available,")
            print("  and it is exactly the incomplete view that makes a wallet")
            print("  look lossless.")
            return 0

        async def main():
            async with httpx.AsyncClient(
                    timeout=httpx.Timeout(60.0, connect=12.0)) as http:
                reader = CTFReader(rpc, http,
                                   chunk_blocks=cfg.polymarket.ctf_chunk_blocks)
                return await reader.summarise(
                    target, from_block=cfg.polymarket.ctf_from_block)

        print("  Reading Conditional-Token events from Polygon…")
        summary = asyncio.run(main())
        true = TruePnL(wallet=target, clob_bought=clob_bought,
                       clob_sold=clob_sold, ctf=summary)

        print()
        print("  ON-CHAIN FLOWS THE ORDER BOOK NEVER SHOWED")
        print(f"    split paid (bought sets) : ${summary.split_paid:,.2f} "
              f"({summary.splits} splits)")
        print(f"    merge received (exits)   : ${summary.merge_received:,.2f} "
              f"({summary.merges} merges)")
        print(f"    redeemed (winners)       : ${summary.redeem_received:,.2f} "
              f"({summary.redemptions} redemptions)")
        print(f"    transfers in / out       : {summary.transfers_in} / "
              f"{summary.transfers_out}")
        print()
        print(f"  CLOB-only P&L : ${true.clob_only_pnl:,.2f}")
        print(f"  TRUE P&L      : ${true.true_pnl:,.2f}")
        print(f"  hidden by the CLOB-only view: ${true.hidden:,.2f}")
        print()
        print("  ^ that difference is the point: the CLOB tape misses the")
        print("    merges and redemptions, which is why the wallet looked like")
        print("    it only ever won.")
        return 0
    finally:
        store.close()


def cmd_history(args) -> int:
    """CLOB price (probability) history for one outcome token."""
    import asyncio

    import httpx

    from .adapters.data_adapter import PolymarketDataAdapter

    cfg = _load(args)
    log = _logger(cfg)

    async def main():
        async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0, connect=12.0)) as http:
            data = PolymarketDataAdapter(cfg, log, http=http)
            return await data.prices_history(args.token, interval=args.interval,
                                             fidelity=args.fidelity)

    series = asyncio.run(main())
    if not series:
        print(f"No price history returned for {args.token}.")
        return 1
    lo = min(p for _t, p in series)
    hi = max(p for _t, p in series)
    print(f"  points   : {len(series)}")
    print(f"  first    : {series[0][1]:.3f}")
    print(f"  last     : {series[-1][1]:.3f}")
    print(f"  min / max: {lo:.3f} / {hi:.3f}")
    return 0


def cmd_funnel(args) -> int:
    """§23: the research engine's own health, top to bottom.

    Every number here is read from persisted state, never recomputed for
    display — a dashboard that recalculates is a dashboard that can disagree
    with the machine it is describing.
    """
    import json as _json

    from .library import StrategyLibrary

    cfg = _load(args)
    try:
        discovery = _json.loads(
            (cfg.data_dir / "discovery.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        discovery = {}
    funnel = discovery.get("funnel") or discovery
    health: dict = {}
    library_path = cfg.data_dir / "library.sqlite3"
    if library_path.exists():
        library = StrategyLibrary(library_path)
        try:
            health = library.attempt_health()
        finally:
            library.close()

    def line(label, value, note=""):
        print(f"  {label:<34} {value:>10}   {note}")

    print("=" * 74)
    print("  RESEARCH FUNNEL")
    print("=" * 74)
    print("\n  SUPPLY")
    line("settled markets known", funnel.get("settledMarkets", 0))
    line("series considered this pass", funnel.get("seriesConsidered", 0))
    line("series admitted", funnel.get("seriesAdmitted", 0))
    line("series rejected", funnel.get("seriesRejected", 0))
    for reason, count in sorted((funnel.get("seriesRejectedBy")
                                 or {}).items()):
        line(f"  - {reason.replace('_', ' ')}", count)
    # `eligiblePoolMarkets` lives inside the health block, not at the top of
    # the funnel. Read from the wrong level it silently returned 0 while the
    # temporal mix on the next line reported 70 markets — two numbers on one
    # screen disagreeing, which is worse than either being absent.
    health_block = funnel.get("health") or {}
    line("eligible OOS markets", health_block.get("eligiblePoolMarkets",
                                                  funnel.get(
                                                      "oosPoolSeries", 0)))
    mix = funnel.get("poolTemporalMixNow") or {}
    if mix:
        line("  temporal mix (now)", "",
             ", ".join(f"{k}={v}" for k, v in sorted(mix.items())))

    print("\n  DISCOVERY")
    line("families this pass", len(funnel.get("familiesThisPass") or {}))
    line("registered this pass", funnel.get("registeredThisPass", 0))
    line("refused: feature incompatible",
         funnel.get("skippedFeatureIncompatible", 0))
    line("refused: duplicate family",
         funnel.get("skippedAsDuplicateFamily", 0))
    line("quarantined this pass", funnel.get("quarantinedThisPass", 0))
    line("library records", funnel.get("libraryRecords", 0))

    print("\n  OOS HEALTH")
    line("replays allocated", funnel.get("oosAllocations", 0))
    line("  of which produced evidence", funnel.get("newIndependentEvents", 0))
    line("  of which never fired", funnel.get("zeroTradeAttempts", 0),
         "non-observations - markets NOT consumed")
    line("  of which failed", funnel.get("replayFailures", 0),
         "DATA_FAILURE, visible not silent")
    line("evidence-bearing markets", health.get("evidenceAttempts", 0))
    line("retryable markets", health.get("retryableMarkets", 0))
    line("candidates with zero OOS markets",
         health.get("candidatesWithZeroOosMarkets", 0))
    line("avg OOS markets / candidate",
         health.get("avgOosMarketsPerCandidate", 0))
    line("median OOS markets / candidate",
         health.get("medianOosMarketsPerCandidate", 0))
    line("max OOS markets / candidate",
         health.get("maxOosMarketsPerCandidate", 0))
    line("forward-OOS evidence markets",
         funnel.get("forwardEvidenceMarkets", 0),
         "walk-forward, not merely unseen")

    print("\n  ALLOCATION")
    line("slate size", funnel.get("allocatedTotal", 0))
    line("  exploration (never tested)", funnel.get("allocatedExploration", 0))
    line("  near misses", funnel.get("allocatedNearMiss", 0))
    line("  exploitation", funnel.get("allocatedExploitation", 0))
    if funnel.get("rewardScored"):
        line("candidates scored for priority", funnel.get("rewardScored", 0))
        line("  mean research reward", funnel.get("rewardMean", 0.0))
        line("  explicitly deprioritised", funnel.get("rewardStopped", 0))

    # §13's research-health block. Kept separate from the OOS numbers above
    # on purpose: these describe how hard the system has tried to break its
    # own findings, and reading them next to evidence counts is how a
    # research signal starts looking like a result.
    if funnel.get("adversarialCandidatesAttacked") is not None:
        print("\n  ADVERSARIAL SELF-CHALLENGE")
        line("candidates attacked",
             funnel.get("adversarialCandidatesAttacked", 0))
        line("tests run", funnel.get("adversarialTestsRun", 0))
        line("  battery coverage",
             f"{float(funnel.get('adversarialMeanCoverage', 0.0)):.0%}",
             "unrunnable tests count AGAINST this")
        line("individual failures", funnel.get("adversarialTestsFailed", 0))
        line("mean robustness", funnel.get("adversarialMeanRobustness", 0.0))
        line("inverse performed better",
             funnel.get("adversarialInverseWon", 0),
             "a finding, not a rejection")
        for verdict, count in sorted(
                (funnel.get("adversarialByVerdict") or {}).items()):
            line(f"  {verdict.lower()}", count)
        for name, count in list(
                (funnel.get("adversarialFailuresByTest") or {}).items())[:5]:
            line(f"  failed: {name}", count)

    if funnel.get("experimentSubjects"):
        print("\n  EXPERIMENT MEMORY")
        line("candidates with a record", funnel.get("experimentSubjects", 0))
        line("classifications stored", funnel.get("experimentsRecorded", 0))
        line("open research directives",
             funnel.get("openResearchDirectives", 0))
        line("throttled families", funnel.get("deadEndFamilies", 0),
             "a throttle, never a ban")
        for reason, count in sorted(
                (funnel.get("experimentFailureReasons") or {}).items(),
                key=lambda kv: -kv[1])[:8]:
            line(f"  {reason}", count)

    hypotheses_attacked = funnel.get("hypothesesAttackedThisPass")
    if hypotheses_attacked is not None:
        print("\n  HYPOTHESIS LAYER")
        line("hypotheses tracked", funnel.get("hypothesesTotal", 0))
        line("convergence groups", funnel.get("convergenceGroups", 0))
        line("  max independent sources",
             funnel.get("convergenceIndependentMax", 0),
             "raises research priority, never evidence")
        line("carrying adversarial results", hypotheses_attacked)
        line("composites registered", funnel.get("compositesRegistered", 0),
             "new candidates, zero inherited evidence")

    print("\n  OUTCOMES")
    line("insufficient evidence", funnel.get("insufficientEvidence", 0))
    line("near miss", funnel.get("nearMiss", 0))
    line("blocked on breadth", funnel.get("blockedOnBreadth", 0))
    line("TRADABLE", funnel.get("tradable", 0))

    sources = funnel.get("discoverySources") or {}
    if sources:
        print("\n  DISCOVERY SOURCES")
        print(f"  {'engine':<26}{'candidates':>12}{'tested':>9}"
              f"{'surviving':>11}{'rejected':>10}")
        for name, row in sorted(sources.items()):
            print(f"  {name:<26}{row.get('candidates', 0):>12}"
                  f"{row.get('tested', 0):>9}{row.get('surviving', 0):>11}"
                  f"{row.get('rejected', 0):>10}")

    print("\n  Zero validated strategies is a legitimate answer. This report")
    print("  describes the research machine, not its conclusions.\n")
    return 0


def cmd_lab(args) -> int:
    """§13: why is this candidate receiving more research, and why was that
    one stopped — answered per candidate, from persisted state.

    Everything printed here was written by the pass that acted on it. The
    alternative — recomputing the explanation at display time — produces a
    report that can disagree with the decision it claims to describe, and
    the disagreement is invisible because both halves look reasonable.
    """
    import json as _json

    cfg = _load(args)
    root = cfg.data_dir / "research"

    def _read(name: str, default):
        try:
            return _json.loads((root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    priorities = _read("research-priority.json", [])
    attacks = {row.get("candidate"): row
               for row in _read("adversarial.json", [])}
    directives = _read("research-directives.json", [])

    if not priorities and not attacks and not directives:
        print("No research-layer output yet - run `pqb research` first.")
        return 1

    print("=" * 78)
    print("  RESEARCH LAB - what is being researched, and why")
    print("=" * 78)

    if priorities:
        print("\n  THIS PASS'S SLATE, in the order it was allocated\n")
        for row in priorities[:int(getattr(args, "limit", 12))]:
            cid = str(row.get("candidate") or "")
            print(f"  {cid[:56]}   reward {row.get('score', 0.0):.3f}")
            if row.get("whyMoreResearch"):
                print(f"      + {row['whyMoreResearch']}")
            if row.get("whyStopped"):
                print(f"      - {row['whyStopped']}")
            attack = attacks.get(cid)
            if attack:
                failed = ", ".join(attack.get("failed") or []) or "none"
                print(f"      attacked: {attack.get('verdict')} "
                      f"({attack.get('testsRun', 0)} test(s), "
                      f"{float(attack.get('coverage', 0.0)):.0%} coverage, "
                      f"robustness {attack.get('robustness', 0.0):.2f}); "
                      f"failed: {failed}")
            print()

    broken = [row for row in attacks.values() if row.get("failed")]
    if broken:
        print(f"\n  BROKEN UNDER ATTACK ({len(broken)})")
        print("  These are still in the library with their full record. An")
        print("  adversarial failure lowers research priority; it is not a")
        print("  verdict, and the status ladder has not been told about it.\n")
        for row in broken[:int(getattr(args, "limit", 12))]:
            print(f"  {str(row.get('candidate'))[:56]}")
            for name in (row.get("failed") or [])[:3]:
                print(f"      {name}: "
                      f"{(row.get('details') or {}).get(name, '')[:88]}")

    if directives:
        print(f"\n  OPEN RESEARCH DIRECTIVES ({len(directives)})")
        print("  What each failure asks the search to try next.\n")
        for row in directives[:int(getattr(args, "limit", 12))]:
            print(f"  {str(row.get('candidate_id'))[:44]:<46}"
                  f"{row.get('failure_reason', ''):<28}"
                  f"{row.get('directive', '')}")
            if row.get("lesson"):
                print(f"      {row['lesson'][:96]}")

    print("\n  None of the above can promote anything. Research priority and")
    print("  validation are separate systems, and this report describes the")
    print("  first one only.\n")
    return 0


def cmd_meta(args) -> int:
    """§10: which research STRUCTURES have survived unseen data.

    Not which rules work — which KINDS of research produce rules that work.
    The output steers where future compute goes and can promote nothing.
    """
    from . import meta as meta_mod
    from .library import StrategyLibrary

    cfg = _load(args)
    path = cfg.data_dir / "library.sqlite3"
    if not path.exists():
        print("No library yet - discovery has not run.")
        return 1
    library = StrategyLibrary(path)
    try:
        rows = []
        for row in library.all_strategies():
            cumulative = library.cumulative(row["id"])
            rows.append({
                "id": row["id"], "rule": row.get("rule") or {},
                "source": row.get("source") or "",
                "family": row.get("family") or "", "status": row["status"],
                "oos_trades": cumulative["trades"],
                "oos_markets": cumulative["markets"],
                "oos_expectancy": cumulative["expectancy"],
                "oos_forward_markets": cumulative.get("forward_markets", 0)})
    finally:
        library.close()

    records = meta_mod.measure(rows)
    table = meta_mod.weights(records)
    summary = meta_mod.summary(records, table)

    print("=" * 78)
    print("  META-DISCOVERY - which kinds of research survive")
    print("=" * 78)
    print(f"\n  structures tracked        {summary['metaStructures']}")
    print(f"  with enough record        "
          f"{summary['metaStructuresWithStanding']}"
          f"   (>= {meta_mod.MIN_CANDIDATES} candidates, "
          f">= {meta_mod.MIN_EVIDENCE_MARKETS} evidence markets)")
    print(f"  actually steering         {summary['metaStructuresSteering']}")

    if not summary["metaHasOpinion"]:
        print("\n  No structure has enough of a record to steer research yet.")
        print("  That is the honest answer, not a failure: weights stay at")
        print("  1.00 until evidence exists, so nothing is amplified early.\n")
        return 0

    entries = meta_mod.report(records, table,
                              min_candidates=args.min_candidates)
    print(f"\n  {'structure':<34}{'cand':>6}{'tested':>8}{'survived':>10}"
          f"{'rate':>8}{'weight':>9}")
    for entry in entries[:args.limit]:
        mark = " " if entry["standing"] else "."
        print(f" {mark}{entry['structure']:<34}{entry['candidates']:>6}"
              f"{entry['tested']:>8}{entry['surviving']:>10}"
              f"{entry['survivalRate']:>8.0%}{entry['weight']:>9.2f}")
    print("\n  '.' marks a structure without enough record to steer; it is")
    print("  shown for context and its weight is held at 1.00.")
    print("\n  Weights move research PRIORITY only. They cannot promote a")
    print("  candidate, and the exploration reserve is untouched by them -")
    print("  a never-tested candidate keeps its slot however unfashionable")
    print("  its structure.\n")
    return 0


def cmd_hypotheses(args) -> int:
    """§21: the hypothesis layer's own panel, kept apart from validation.

    Nothing on this screen is a validation result. SUPPORTED here means a
    proposed relationship survived inversion and adversarial testing; it has
    passed no OOS gate and cannot trade.
    """
    from .hypothesis import HypothesisStore

    cfg = _load(args)
    path = cfg.data_dir / "research" / "hypotheses.sqlite3"
    if not path.exists():
        print(f"No hypothesis store yet at {path}.")
        print("Run `python -m pqb.cli research` first.")
        return 0
    store = HypothesisStore(path)
    try:
        summary = store.summary()
        rows = store.all()
        groups = store.groups()
    finally:
        store.close()

    print("=" * 74)
    print("  HYPOTHESES - research signal only, never validation")
    print("=" * 74)
    print(f"\n  tracked: {summary['hypothesesTotal']}")
    for status, count in sorted((summary["hypothesesByStatus"]).items()):
        if count:
            print(f"    {status:<24} {count}")

    print(f"\n  CONVERGENCE")
    print(f"    groups                   {summary['convergenceGroups']}")
    print(f"    most independent sources {summary['convergenceIndependentMax']}"
          "   (same-source repetition does not count)")
    print(f"    avg sources per group    {summary['convergenceAvgSources']}")

    print(f"\n  ADVERSARIAL")
    print(f"    attacked                 {summary['adversarialTested']}")
    print(f"    weakened                 {summary['adversarialWeakened']}")
    print(f"    rejected                 {summary['adversarialRejected']}")
    print(f"    inverse won              {summary['inverseWinners']}"
          "   (real, and backwards)")
    print(f"    failure states found     {summary['failureStatesFound']}")
    print(f"    promoted to candidate    {summary['promotedToCandidate']}"
          "   (queued for OOS like anything else)")

    if groups:
        print("\n  STRONGEST CONVERGENCE GROUPS")
        for group in groups[:args.limit]:
            print(f"    [{group['independent']} independent] "
                  f"{group['pattern'][:60]}")
            print(f"      sources: {', '.join(group['sources'])}")

    ranked = [h for h in rows if h.priority > 0][:args.limit]
    if ranked:
        print("\n  HIGHEST RESEARCH PRIORITY")
        print("  (what to investigate harder - NOT what works)")
        for hypothesis in ranked:
            print(f"    {hypothesis.priority:>7.4f}  [{hypothesis.status}] "
                  f"{hypothesis.relationship[:52]}")
            print(f"             {hypothesis.supporting} for / "
                  f"{hypothesis.contradicting} against, "
                  f"{hypothesis.independent_markets} markets, "
                  f"{len(hypothesis.sources)} source(s)")
            if hypothesis.failure_states:
                print(f"             fails when: "
                      f"{'; '.join(hypothesis.failure_states[:2])}")
    print()
    return 0


def cmd_quality(args) -> int:
    """The master spec's report split: signal, entry, exit and sizing quality,
    each judged separately so a weak link is visible instead of averaged away."""
    import sqlite3

    cfg = _load(args)
    conn = sqlite3.connect(str(cfg.journal_path))
    conn.row_factory = sqlite3.Row

    def q(sql, params=()):
        try:
            return [dict(r) for r in conn.execute(sql, params)]
        except sqlite3.OperationalError:
            return []

    try:
        print("=" * 74)
        print("  QUALITY REPORT - each stage judged on its own")
        print("=" * 74)

        # 1. SIGNAL: did higher conviction actually mean better outcomes?
        rows = q("""SELECT d.score, l.realized_pnl FROM lifecycles l
                    JOIN decisions d ON d.id = l.entry_decision_id
                    WHERE l.status='CLOSED'""")
        print("\n  1. SIGNAL QUALITY - conviction vs what actually happened")
        if len(rows) < args.min_count:
            print(f"     not enough closed trades yet ({len(rows)}).")
        else:
            for lo, hi in ((0.0, 0.55), (0.55, 0.7), (0.7, 0.85), (0.85, 1.01)):
                bucket = [r for r in rows if lo <= (r["score"] or 0) < hi]
                if not bucket:
                    continue
                wins = sum(1 for r in bucket if (r["realized_pnl"] or 0) > 0)
                pnl = sum(r["realized_pnl"] or 0 for r in bucket)
                print(f"     score {lo:.2f}-{hi:.2f}: {len(bucket):>4} trades, "
                      f"{wins / len(bucket):>4.0%} won, ${pnl:>+8.2f}")
            print("     (a real signal: higher buckets should not do worse)")

        # 2. ENTRY: what we paid vs what we asked for.
        rows = q("""SELECT limit_price, avg_price FROM executions
                    WHERE side='BUY' AND filled_size > 0 AND limit_price > 0""")
        print("\n  2. ENTRY QUALITY - fills vs intent")
        if not rows:
            print("     no fills recorded yet.")
        else:
            slip = [(r["avg_price"] - r["limit_price"]) / r["limit_price"]
                    for r in rows]
            print(f"     {len(rows)} entries; mean slippage "
                  f"{sum(slip) / len(slip):+.3%}, worst {max(slip):+.3%}")

        # 3. EXIT: which ways out actually made money.
        rows = q("""SELECT exit_style, COUNT(*) n, SUM(realized_pnl) pnl,
                           AVG(return_pct) avg_ret
                    FROM lifecycles WHERE status='CLOSED'
                    GROUP BY exit_style ORDER BY pnl DESC""")
        print("\n  3. EXIT QUALITY - by the reason we left")
        for r in rows:
            print(f"     {str(r['exit_style'] or '?'):<14} {r['n']:>4} exits, "
                  f"${(r['pnl'] or 0):>+8.2f}, avg {(r['avg_ret'] or 0):>+6.1%}")
        if not rows:
            print("     no closed trades yet.")

        # 4. MONEY MANAGEMENT: did the sizing put more behind better trades?
        rows = q("""SELECT entry_cost, realized_pnl FROM lifecycles
                    WHERE status='CLOSED' AND entry_cost > 0""")
        print("\n  4. MONEY MANAGEMENT - size vs result")
        if len(rows) < args.min_count:
            print(f"     not enough closed trades yet ({len(rows)}).")
        else:
            rows.sort(key=lambda r: r["entry_cost"])
            half = len(rows) // 2
            small, large = rows[:half], rows[half:]
            for label, part in (("smaller half", small), ("larger half", large)):
                ret = sum(r["realized_pnl"] for r in part) / \
                    sum(r["entry_cost"] for r in part)
                print(f"     {label:<13} {len(part):>4} trades, "
                      f"return on stake {ret:+.1%}")
            print("     (sizing works when the larger half is not the worse half)")
        print()
        return 0
    finally:
        conn.close()


def cmd_calibration(args) -> int:
    """Evidence that the decision model's edge is real, before any live money."""
    cfg = _load(args)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from calibration_report import report
    return report(cfg.journal_path, min_count=args.min_count)


def cmd_gate(args) -> int:
    """Would live trading be allowed right now? The code-enforced gate's verdict."""
    cfg = _load(args)
    from .decision.live_gate import evaluate as evaluate_gate

    result = evaluate_gate(cfg.journal_path, cfg.live_gate)
    print("=" * 70)
    print("  LIVE-EXECUTION GATE")
    print("=" * 70)
    print(f"  verdict : {'PASS - live would be allowed' if result.passed else 'BLOCKED - paper only'}")
    print()
    print("  measured")
    for key, value in result.metrics.items():
        print(f"    {key:<16}: {value}")
    if result.reasons:
        print()
        print("  still blocked because")
        for reason in result.reasons:
            print(f"    - {reason}")
    print()
    print("  This is enforced in code: a live start with an unmet gate is")
    print("  downgraded to paper automatically. Tune it under live_gate: in")
    print("  the config.")
    return 0 if result.passed else 1


def cmd_report(args) -> int:
    cfg = _load(args)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from analyze_journal import report
    report(cfg.journal_path, min_count=args.min_count)
    return 0


# --- parser -----------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pqb", description="Polymarket Quant Bridge")
    parser.add_argument("--config", default=DEFAULT_CONFIG,
                        help=f"Config file (default {DEFAULT_CONFIG}).")
    subs = parser.add_subparsers(dest="command", required=True)

    check = subs.add_parser("check", help="Validate the config. No network I/O.")
    check.add_argument("--show", action="store_true",
                       help="Print the resolved config (secrets redacted).")
    check.set_defaults(func=cmd_check)

    run = subs.add_parser("run", help="Run the evaluation loop.")
    run.add_argument("--dry-run", action="store_true",
                     help="Force dry-run regardless of the config.")
    run.add_argument("--cycles", type=int, default=0,
                     help="Run N cycles then exit (0 = forever).")
    run.add_argument("--yes-live", action="store_true",
                     help="Pre-authorise live trading when no console is "
                          "attached (e.g. the dashboard). Ignored in dry-run.")
    run.set_defaults(func=cmd_run)

    status = subs.add_parser("status", help="Show persisted engine state.")
    status.set_defaults(func=cmd_status)

    kill = subs.add_parser("kill", help="Arm the kill switch.")
    kill.add_argument("--flatten", action="store_true",
                      help="Also close every open position.")
    kill.set_defaults(func=cmd_kill)

    resume = subs.add_parser("resume",
                             help="Clear the kill switch / reconciliation halt.")
    resume.add_argument("--force", action="store_true",
                        help="Also clear a reconciliation halt (review first).")
    resume.set_defaults(func=cmd_resume)

    report = subs.add_parser("report", help="Aggregate the decision journal.")
    report.add_argument("--min-count", type=int, default=1,
                        help="Hide groups with fewer than N records.")
    report.set_defaults(func=cmd_report)

    wallets = subs.add_parser(
        "wallets", help="The dynamic wallet ranking, as the system derived it.")
    wallets.add_argument("--limit", type=int, default=40,
                         help="Ranked wallets to show (default 40).")
    wallets.set_defaults(func=cmd_wallets)

    anomalies = subs.add_parser(
        "anomalies", help="Detections, with the evidence behind each.")
    anomalies.add_argument("--limit", type=int, default=20,
                           help="Most recent detections to show.")
    anomalies.add_argument("--kind", default="",
                           help="Filter to one kind (see analytics.anomalies).")
    anomalies.set_defaults(func=cmd_anomalies)

    research = subs.add_parser(
        "research",
        help="Discover strategies from captured features via the Quant Bridge.")
    research.add_argument("--min-rows", type=int, default=0,
                          help="Captured rows a token needs to be researched.")
    research.add_argument("--max-tokens", type=int, default=0,
                          help="Token series to research this run.")
    research.add_argument("--min-tokens", type=int, default=0,
                          help="Tokens a rule must be accepted on to be kept.")
    research.set_defaults(func=cmd_research)

    calibration = subs.add_parser(
        "calibration",
        help="Is the edge real? Calibration, EV and drawdown before live money.")
    calibration.add_argument("--min-count", type=int, default=3,
                             help="Hide buckets with fewer than N settled.")
    calibration.set_defaults(func=cmd_calibration)

    gate = subs.add_parser(
        "gate",
        help="Would live trading be allowed now? The code-enforced gate verdict.")
    gate.set_defaults(func=cmd_gate)

    funnel_cmd = subs.add_parser(
        "funnel",
        help="The research engine's own health: supply, discovery, OOS, "
             "allocation, bottlenecks.")
    funnel_cmd.set_defaults(func=cmd_funnel)

    meta_cmd = subs.add_parser(
        "meta",
        help="Which research STRUCTURES survive unseen data - engines, "
             "feature families, sequence lengths, holding periods.")
    meta_cmd.add_argument("--limit", type=int, default=25,
                          help="Rows to show.")
    meta_cmd.add_argument("--min-candidates", type=int, default=3,
                          help="Hide structures with fewer candidates.")
    meta_cmd.set_defaults(func=cmd_meta)

    lab = subs.add_parser(
        "lab",
        help="Why each candidate is receiving more research, what deliberate "
             "attack found, and what each failure asks the search to try "
             "next. Research priority only - it promotes nothing.")
    lab.add_argument("--limit", type=int, default=12,
                     help="Rows per section.")
    lab.set_defaults(func=cmd_lab)

    hypotheses = subs.add_parser(
        "hypotheses",
        help="Cross-source convergence, adversarial results and research "
             "priority. Research signal only - never validation.")
    hypotheses.add_argument("--limit", type=int, default=10,
                            help="Rows per section.")
    hypotheses.set_defaults(func=cmd_hypotheses)

    quality = subs.add_parser(
        "quality",
        help="Signal, entry, exit and sizing quality - each judged separately.")
    quality.add_argument("--min-count", type=int, default=5,
                         help="Hide sections with fewer than N closed trades.")
    quality.set_defaults(func=cmd_quality)

    onchain = subs.add_parser(
        "onchain",
        help="True per-wallet P&L: CLOB trades plus on-chain CTF split/merge/"
             "redeem — the flows that make a wallet look lossless.")
    onchain.add_argument("address", help="The 0x... wallet address.")
    onchain.set_defaults(func=cmd_onchain)

    history = subs.add_parser(
        "history", help="CLOB price (probability) history for one token.")
    history.add_argument("token", help="The clob token id.")
    history.add_argument("--interval", default="max",
                         help="max | 1m | 1w | 1d | 6h | 1h (default max).")
    history.add_argument("--fidelity", type=int, default=0,
                         help="Resolution in minutes (0 = endpoint default).")
    history.set_defaults(func=cmd_history)

    wallet = subs.add_parser(
        "wallet", help="Reverse-engineer one wallet's method from its history.")
    wallet.add_argument("address", help="The 0x... wallet address.")
    wallet.add_argument("--by-market", action="store_true",
                        help="Split the wallet into its per-category strategies.")
    wallet.set_defaults(func=cmd_wallet)

    lifecycle = subs.add_parser(
        "lifecycle",
        help="How a wallet manages a market: entry, adds, and the flip to the "
             "opposite outcome. The position-management view, not the buy list.")
    lifecycle.add_argument("address", help="The 0x... wallet address.")
    lifecycle.set_defaults(func=cmd_lifecycle)

    strategies = subs.add_parser(
        "strategies",
        help="Per-market best exit rule for each top wallet (not one per wallet).")
    strategies.add_argument("--address", default="",
                            help="One wallet, broken out by market.")
    strategies.add_argument("--limit", type=int, default=25,
                            help="Ranked wallets to analyse (default 25).")
    strategies.add_argument("--min-positions", type=int, default=4,
                            help="Settled positions a segment needs (default 4).")
    strategies.set_defaults(func=cmd_strategies)

    backfill = subs.add_parser(
        "backfill",
        help="Pull already-settled market history so wallets can be studied now.")
    backfill.add_argument("--markets", type=int, default=60,
                          help="Closed markets to pull (default 60).")
    backfill.add_argument("--trades", type=int, default=500,
                          help="Trades per market (default 500).")
    backfill.add_argument("--max-volume", type=float, default=None,
                          help="Skip markets above this volume: their tape is "
                               "longer than the API pages, so only the "
                               "already-decided tail is reachable. 0 disables.")
    backfill.add_argument("--min-volume", type=float, default=None,
                          help="Skip markets below this volume - too quiet to "
                               "have a studiable tape. 0 disables.")
    backfill.set_defaults(func=cmd_backfill)

    cascade = subs.add_parser(
        "cascade",
        help="Liquidation-cascade event study: direction test, response "
             "curve vs baseline, size buckets. Research only.")
    cascade.set_defaults(func=cmd_cascade)

    strategy = subs.add_parser(
        "strategy",
        help="One candidate's full audit trail: markets tested, when, "
             "results, blockers. Pass any unique part of its id.")
    strategy.add_argument("id", help="Strategy id or unique fragment.")
    strategy.set_defaults(func=cmd_strategy)

    families = subs.add_parser(
        "families",
        help="Hypothesis families: evidence across versions, "
             "market-deduplicated. Research interpretation only.")
    families.set_defaults(func=cmd_families)

    motifs = subs.add_parser(
        "motifs",
        help="Recurring STRUCTURE across families, with evidence "
             "provenance: which structural classes replicate on independent "
             "markets and which recur only in failure. Research only.")
    motifs.add_argument("--limit", type=int, default=20,
                        help="Motifs to show (default 20).")
    motifs.add_argument("--questions", action="store_true",
                        help="Also print the counterfactual questions each "
                             "motif implies.")
    motifs.set_defaults(func=cmd_motifs)

    walletstates = subs.add_parser(
        "walletstates",
        help="Wallet behavioral states (the RN1 model): first-buy band + "
             "checkpoint-frozen one-sidedness, per wallet. Research only.")
    walletstates.set_defaults(func=cmd_walletstates)

    walletbehavior = subs.add_parser(
        "walletbehavior",
        help="Wallet behavioral discovery: reconstruct ranked wallets' "
             "settled trades, extract the repeating behavior as wallet-free "
             "strategy hypotheses. Research only.")
    walletbehavior.set_defaults(func=cmd_walletbehavior)

    longshot = subs.add_parser(
        "longshot",
        help="Military-attack longshot calibration: implied vs realized "
             "probability, military vs control. Research only.")
    longshot.set_defaults(func=cmd_longshot)

    activity = subs.add_parser(
        "activity",
        help="Backfill non-trade wallet activity (REDEEM / MERGE / SPLIT) "
             "from the Data API's /activity feed. /trades does not carry "
             "these, which is why the lifecycle layer reports them missing.")
    activity.add_argument("--wallets", type=int, default=500,
                          help="Busiest wallets to collect (default 500).")
    activity.add_argument("--min-trades", type=int, default=20,
                          help="Skip wallets below this many trades.")
    activity.add_argument("--pages", type=int, default=4,
                          help="Activity pages per wallet (500/page).")
    activity.add_argument("--pause", type=float, default=0.15,
                          help="Seconds between wallets.")
    activity.set_defaults(func=cmd_activity)

    settle = subs.add_parser(
        "settlements",
        help="Drain the settlement backlog off the trading loop. Resolves "
             "how traded markets ended, which is what turns pending "
             "predictions into graded ones and simulated entries into "
             "realised P&L.")
    settle.add_argument("--batch", type=int, default=60,
                        help="Markets per request batch (default 60).")
    settle.add_argument("--max-batches", type=int, default=0,
                        help="Stop after this many batches. 0 = drain it.")
    settle.add_argument("--pause", type=float, default=0.4,
                        help="Seconds between batches (default 0.4).")
    settle.add_argument("--patience", type=int, default=10,
                        help="Consecutive empty batches before stopping. "
                             "Unresolved markets cluster, so one empty batch "
                             "is a cluster, not the end of the backlog.")
    settle.set_defaults(func=cmd_settlements)

    wsr = subs.add_parser(
        "wallet-state-research",
        help="Wallet State Transition Research: reproduce the frozen RN1 "
             "post-opposite-buy rule, test whether it generalises across "
             "wallets and markets, and whether it is tradable after costs. "
             "Read-only; changes nothing.")
    wsr.add_argument("--horizon", type=float, default=0.0,
                     help="Signal horizon in minutes (default: config, 3).")
    wsr.add_argument("--rn1-only", action="store_true",
                     help="Only the exact frozen reproduction; skip the "
                          "cross-wallet and cross-market studies.")
    wsr.add_argument("--discovery", action="store_true",
                     help="Also fit and compare the model families "
                          "(development window only).")
    wsr.add_argument("--max-wallets", type=int, default=0,
                     help="Cap wallets studied, busiest first. RN1 is always "
                          "included. 0 = every wallet.")
    wsr.add_argument("--structure", action="store_true",
                     help="Also run the MDL hidden-structure search. The "
                          "null model is allowed to win, and usually does.")
    wsr.add_argument("--out", default="",
                     help="Where to write the reports.")
    wsr.add_argument("--json", action="store_true",
                     help="Print the dashboard summary as JSON.")
    wsr.add_argument("--quiet", action="store_true",
                     help="Suppress progress output.")
    wsr.set_defaults(func=cmd_wallet_state_research)

    forensics_cmd = subs.add_parser(
        "forensics",
        help="Money-management diagnostics: exit/entry/hold/size/cost "
             "attribution, post-exit counterfactuals, and the research "
             "hypotheses they imply. Read-only; changes nothing.")
    forensics_cmd.add_argument("--json", action="store_true",
                               help="Emit the full report as JSON.")
    forensics_cmd.add_argument("--write", action="store_true",
                               help="Also write state/forensics.json.")
    forensics_cmd.set_defaults(func=cmd_forensics)

    attribution = subs.add_parser(
        "attribution",
        help="Where profit is gained and leaked: price/hold/TTR buckets, "
             "execution quality, skip reasons. Read-only research.")
    attribution.set_defaults(func=cmd_attribution)

    playbook = subs.add_parser(
        "playbook",
        help="The exit rule that would have worked best for each ranked wallet.")
    playbook.add_argument("--address", default="",
                          help="One wallet, with every rule tested shown.")
    playbook.add_argument("--limit", type=int, default=25,
                          help="Ranked wallets to analyse (default 25).")
    playbook.set_defaults(func=cmd_playbook)

    resize = subs.add_parser(
        "resize-library",
        help="Void validation evidence recorded at the pre-fix account "
             "size. Run once, after the sizing fix, before the next pass.")
    resize.add_argument("--yes", action="store_true",
                        help="Actually do it. Without this, only reports.")
    resize.add_argument("--force", action="store_true",
                        help="Reset again even though an epoch is already "
                             "stamped (only if the account size changed).")
    resize.add_argument("--note", default="",
                        help="Recorded alongside the epoch, e.g. why.")
    resize.set_defaults(func=cmd_resize_library)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
