"""pqv2 — the V2 command line. Read-only against the original installation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import gatemap
from .audit import Paths, render, report
from .router import load_validated

# Defaults point at the repo's data folder and the original engine. Both are
# overridable, and neither is ever opened for writing by this package.
DEFAULT_DATA = Path(
    os.environ.get("PQV2_DATA_DIR")
    or Path(__file__).resolve().parents[2] / "Polymarket-Bot-DATA" / "state")
DEFAULT_ENGINE = Path(
    os.environ.get("PQV2_ENGINE_ROOT")
    or Path(__file__).resolve().parents[2] / "Polymarket-Bot-DAVID"
    / "polymarket-quant-bridge" / "pqb")
DEFAULT_WORK = Path(
    os.environ.get("PQV2_WORK_DIR")
    or Path(__file__).resolve().parents[1] / "state")


def cmd_audit(args) -> int:
    paths = Paths.discover(args.data, ledger=args.ledger
                           or (DEFAULT_WORK / "opportunities.sqlite3"))
    data = report(paths, args.engine)
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        print(render(data))
    return 0


def cmd_gates(args) -> int:
    data = gatemap.summary()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print("GATE OWNERSHIP MAP")
    print("=" * 74)
    print("Only GLOBAL_SAFETY / PORTFOLIO / EXECUTION may block both routes.\n")
    for owner in gatemap.OWNERS:
        rows = gatemap.gates_for(owner)
        if not rows:
            continue
        both = "blocks BOTH routes" if owner in (
            gatemap.GLOBAL_SAFETY, gatemap.PORTFOLIO,
            gatemap.EXECUTION) else f"blocks route {owner[-1]} ONLY"
        print(f"{owner}  ({len(rows)} gates — {both})")
        for g in rows:
            print(f"   {g.key:<22} {g.summary}")
            print(f"   {'':<22} source: {g.source}")
            if g.measured:
                print(f"   {'':<22} MEASURED: {g.measured}")
        print()
    return 0


def cmd_strategies(args) -> int:
    """What route B has that is actually validated."""
    registry = Path(args.data) / "walletlab" / "experiments.sqlite3"
    specs = load_validated(registry)
    if not specs:
        print(f"No VALIDATED strategies in {registry}.")
        print("This is a correct outcome, not an error.")
        return 0
    print(f"{len(specs)} VALIDATED route-B strateg"
          f"{'y' if len(specs) == 1 else 'ies'} in {registry}\n")
    for s in specs:
        print(f"  {s.strategy_id}")
        print(f"    wallet     : {s.wallet}")
        print(f"    score      : {s.score:.4f}   out-of-sample p: {s.oos_p:.3e}")
        print(f"    price band : {s.min_price} - {s.max_price}")
        print(f"    entry delay: {s.delay_secs:.0f}s after the wallet")
        print(f"    stake      : {s.stake_mode} "
              f"(fraction {s.stake_fraction})")
        for equity in (40.0, 100.0, 1000.0):
            print(f"       on ${equity:,.0f} equity -> "
                  f"${s.stake_for(equity):,.2f}")
        print()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="pqv2",
        description="Polymarket Quant V2 — two-route architecture and the "
                    "opportunity-loss audit. Never writes to the original "
                    "installation.")
    parser.add_argument("--data", default=str(DEFAULT_DATA),
                        help="the original install's state directory")
    parser.add_argument("--engine", default=str(DEFAULT_ENGINE),
                        help="the running engine's package root")
    subs = parser.add_subparsers(dest="command", required=True)

    audit = subs.add_parser(
        "audit", help="Where do the opportunities go? Answers the 22 "
                      "diagnostic questions from the real databases.")
    audit.add_argument("--json", action="store_true")
    audit.add_argument("--ledger", default="")
    audit.set_defaults(func=cmd_audit)

    gates = subs.add_parser(
        "gates", help="Who owns each gate, and which routes it may block.")
    gates.add_argument("--json", action="store_true")
    gates.set_defaults(func=cmd_gates)

    strategies = subs.add_parser(
        "strategies", help="Route B's VALIDATED strategies, and what they "
                           "would stake on a given account.")
    strategies.set_defaults(func=cmd_strategies)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
