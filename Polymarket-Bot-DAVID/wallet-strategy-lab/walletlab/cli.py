"""Command line (§48).

    python -m walletlab inventory              measure the substrate (§26/§49)
    python -m walletlab baselines              naive-copy edge per wallet (§46)
    python -m walletlab analyze-wallet <addr>  deep dive on one wallet
    python -m walletlab discover-strategies    the full pass (§12)
    python -m walletlab leaderboard            what has survived so far (§35)
    python -m walletlab live-signals           validated strategies only (§19/§42)
"""

from __future__ import annotations

import argparse
import json
import sys

from .backtest import run
from .config import settings
from .data import PriceTape, inventory, wallet_trade_counts
from .discover import discover, load_observations
from .registry import Registry
from .report import render_discovery, render_inventory, render_wallet
from .state import stream_features
from .strategy import candidates_for, grid_size, naive_copy


def _dataset_version(st) -> str:
    inv = inventory(st)
    return f"{inv['wallet_trades_total']}:{inv['resolutions']}:{inv['tape_last_ts']}"


def cmd_inventory(args) -> int:
    st = settings(args.db, args.work)
    inv = inventory(st)
    if args.json:
        print(json.dumps(inv, indent=2))
    else:
        print(render_inventory(inv, grid_size()))
    return 0


def cmd_baselines(args) -> int:
    st = settings(args.db, args.work)
    tape = PriceTape(st)
    ranked = wallet_trade_counts(st, min_trades=args.min_trades)[: args.max_wallets]
    wallets = [w for w, _ in ranked]
    print(f"loading causal features for {len(wallets)} wallets ...", file=sys.stderr)
    obs = load_observations(st, wallets)

    rows = []
    for w in wallets:
        o = obs.get(w, [])
        if not o:
            continue
        r = run(naive_copy(w), o, st, tape)
        if r.n_filled >= args.min_trades // 2:
            rows.append((w, r))
    rows.sort(key=lambda x: -x[1].expectancy)

    print(f"\n{'wallet':44s} {'n':>5} {'expect':>8} {'roi':>8} {'win':>6} {'t':>7} {'mkts':>5}")
    print("-" * 90)
    for w, r in rows:
        print(f"{w:44s} {r.n_filled:5d} {r.expectancy:+8.4f} {r.roi:+8.4f} "
              f"{r.win_rate:6.3f} {r.t_stat():+7.2f} {len(r.markets):5d}")
    pos = sum(1 for _, r in rows if r.expectancy > 0)
    print(f"\n{pos}/{len(rows)} wallets have positive naive-copy expectancy after costs.")
    print("This is IN-SAMPLE and selection-biased by construction — it is a baseline")
    print("to beat (§46), not a result. Use discover-strategies for validated output.")
    return 0


def cmd_analyze_wallet(args) -> int:
    st = settings(args.db, args.work)
    tape = PriceTape(st)
    obs = load_observations(st, [args.wallet]).get(args.wallet, [])
    if not obs:
        print(f"no settled copyable trades for {args.wallet}")
        return 1
    print(render_wallet(args.wallet, obs, st, tape))
    return 0


def cmd_discover(args) -> int:
    st = settings(args.db, args.work)
    reg = Registry(st.registry_path(), _dataset_version(st))
    try:
        rep = discover(
            st,
            max_wallets=args.max_wallets,
            min_trades=args.min_trades,
            fdr=args.fdr,
            registry=reg,
            log=lambda m: print(m, file=sys.stderr),
        )
    finally:
        reg.close()
    if args.json:
        print(json.dumps(rep, indent=2, default=str))
    else:
        print(render_discovery(rep))
    return 0


def cmd_leaderboard(args) -> int:
    st = settings(args.db, args.work)
    reg = Registry(st.registry_path(), _dataset_version(st))
    try:
        rows = reg.leaderboard(limit=args.limit, status=args.status)
        total = reg.count()
        print(f"registry holds {total:,} experiments\n")
        if not rows:
            print("nothing recorded yet — run discover-strategies first.")
            return 0
        print(f"{'wallet':44s} {'status':22s} {'score':>7} {'oos_p':>9} {'n':>5} {'expect':>8}")
        print("-" * 100)
        for r in rows:
            t = r["test"]
            print(f"{r['wallet']:44s} {r['status']:22s} {r['score'] or 0:7.3f} "
                  f"{r['oos_p'] or 1:9.5f} {t.get('n_filled', 0):5d} "
                  f"{t.get('expectancy', 0):+8.4f}")
    finally:
        reg.close()
    return 0


def cmd_live_signals(args) -> int:
    """§19/§42: only VALIDATED strategies may emit anything."""
    st = settings(args.db, args.work)
    reg = Registry(st.registry_path(), _dataset_version(st))
    try:
        rows = reg.leaderboard(limit=100, status="VALIDATED")
    finally:
        reg.close()
    if not rows:
        print("NO VALIDATED STRATEGIES. No signals emitted.")
        print("This is a correct outcome, not an error (§38).")
        return 0
    for r in rows:
        print(json.dumps({
            "strategy_id": r["spec_hash"], "wallet": r["wallet"],
            "status": "VALIDATED", "score": r["score"], "oos_p": r["oos_p"],
            "conditions": {k: v for k, v in r["spec"].items() if v not in (None, False)},
            "max_notional": st.costs.max_notional,
        }))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="walletlab")
    p.add_argument("--db", default=None, help="path to intel.sqlite3")
    p.add_argument("--work", default=None, help="working directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inventory"); s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_inventory)

    s = sub.add_parser("baselines")
    s.add_argument("--max-wallets", type=int, default=40)
    s.add_argument("--min-trades", type=int, default=100)
    s.set_defaults(fn=cmd_baselines)

    s = sub.add_parser("analyze-wallet"); s.add_argument("wallet")
    s.set_defaults(fn=cmd_analyze_wallet)

    s = sub.add_parser("discover-strategies")
    s.add_argument("--max-wallets", type=int, default=25)
    s.add_argument("--min-trades", type=int, default=100)
    s.add_argument("--fdr", type=float, default=0.10)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("leaderboard")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--status", default=None)
    s.set_defaults(fn=cmd_leaderboard)

    s = sub.add_parser("live-signals"); s.set_defaults(fn=cmd_live_signals)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
