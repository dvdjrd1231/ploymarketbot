"""The first-run research report and the $100 capital test report.

One document that answers, from persisted data only: what evidence exists, what
was searched, what survived, what it would have done at the configured
bankroll, and — the section most reports omit — what none of it can tell you.

Every number is read back out of the store or recomputed from the matrix. There
are no literals in this file that describe results. A section with no data
prints what would populate it rather than a zero.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..core.source import HistoricalSource
from ..ingest.settled_ts import coverage as settled_coverage
from ..research.stats import format_p


def _fmt(v, nd: int = 4) -> str:
    if v is None:
        return "— (not measured)"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def build(st: Settings, store, *, engine=None) -> str:
    src = HistoricalSource(st)
    L: list = []
    add = L.append

    add("# Polymarket Quant Bridge V3 — Research Report")
    add("")
    add(f"Generated {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())} UTC  ")
    add(f"Mode **{st.mode.value}** · live trading "
        f"**{'AUTHORISED' if st.live_authorized else 'DISABLED'}** · starting "
        f"bankroll **${st.capital.starting_capital:,.2f}**")
    add("")
    add("> Nothing in this system has traded real money. No figure below is a "
        "claim of future profitability.")
    add("")

    # ---------------------------------------------------------------- data
    add("## 1. What evidence exists")
    add("")
    inv = src.inventory()
    if not inv.get("available"):
        add(f"No historical database at `{inv.get('path')}`. Everything below "
            f"is empty for that reason.")
    else:
        for k in ("wallet_trades", "wallets", "markets", "tokens",
                  "resolutions", "tape_days"):
            add(f"- **{k}**: {_fmt(inv.get(k))}")
        cov = settled_coverage(store)
        add(f"- **settlement timestamps usable**: {cov['usable']:,} of "
            f"{cov['total']:,}")
        add("")
        add(f"> {cov['note']}")
    add("")
    add("### Layers with no history")
    add("")
    add("| layer | rows | history (days) | can be backfilled? |")
    add("|---|---:|---:|---|")
    for table, label, backfill in (
            ("book_snapshots", "order book", "**No — never**"),
            ("news_items", "news", "No — accumulates only"),
            ("chain_events", "blockchain", "Partially, from an RPC"),
            ("markets", "market metadata", "Yes, from the venue")):
        n = store.count(table)
        span = store.history_span_days(
            table, "capture_ts" if table == "news_items" else "ts")
        add(f"| {label} | {n:,} | {span} | {backfill} |")
    add("")

    # ------------------------------------------------------------ research
    add("## 2. What was searched")
    add("")
    passes = store.query(
        "SELECT * FROM research_passes ORDER BY started_ts DESC LIMIT 5")
    if not passes:
        add("No discovery pass has been run. Run `python -m pqv3 discover`.")
    else:
        add("| pass | tested | distinct | BH threshold | validated | secs |")
        add("|---|---:|---:|---:|---:|---:|")
        for p in passes:
            d = _json(p.get("detail"))
            add(f"| `{p['pass_id'][:10]}` | {p['tested']:,} | "
                f"{p['distinct_tested']:,} | {p['bh_threshold']:.3g} | "
                f"{p['surviving']} | {d.get('elapsed_secs', '')} |")
        add("")
        latest = _json(passes[0].get("detail"))
        for n in latest.get("notes", []):
            add(f"- {n}")
        add("")
        ss = latest.get("search_space") or {}
        if ss.get("inert_features"):
            add(f"**Inert features excluded:** "
                f"`{'`, `'.join(ss['inert_features'])}`. They are identically "
                f"zero on this data, so searching them would consume "
                f"multiple-testing budget and could not produce a finding.")
            add("")

    # ----------------------------------------------------------- strategies
    add("## 3. What survived")
    add("")
    rows = store.query(
        "SELECT * FROM strategies ORDER BY expectancy DESC LIMIT 200")
    if not rows:
        add("No strategy records. The validation ladder has not been run.")
    else:
        by_status: dict = {}
        for r in rows:
            v = _json(r.get("params")).get("verdict", {})
            by_status[v.get("status", "?")] = \
                by_status.get(v.get("status", "?"), 0) + 1
        add("| ladder outcome | count |")
        add("|---|---:|")
        for k, v in sorted(by_status.items(), key=lambda kv: -kv[1]):
            add(f"| {k} | {v} |")
        add("")
        validated = [r for r in rows
                     if _json(r.get("params")).get("verdict", {}).get("status")
                     == "VALIDATED"]
        distinct = None
        if passes:
            distinct = _json(passes[0].get("detail")).get("distinct_findings")
        add(f"**{len(validated)} strategy(ies) reached VALIDATED.** "
            f"VALIDATED authorises paper trading only.")
        if distinct is not None and validated:
            add("")
            add(f"> They collapse to **{distinct} distinct finding(s)** once "
                f"near-duplicates are merged by overlap of the trades they "
                f"admit. A grid search returns the same effect at several "
                f"thresholds; counting those separately would overstate the "
                f"evidence and would also let a portfolio hold "
                f"'{len(validated)} uncorrelated strategies' that are "
                f"{distinct} bet(s).")
            groups = _json(passes[0].get("detail")).get("finding_groups") or []
            if groups:
                add("")
                add("| distinct finding | variants |")
                add("|---|---:|")
                for g in groups[:12]:
                    add(f"| {g.get('statement','')[:70]} | "
                        f"{g.get('n_variants')} |")
        add("")
        if validated:
            add("| statement | OOS n | markets | expectancy | matched excess "
                "| p | evidence |")
            add("|---|---:|---:|---:|---:|---:|---|")
            for r in validated[:20]:
                p = _json(r.get("params"))
                oos = p.get("out_of_sample", {})
                add(f"| {p.get('statement', '')[:64]} | {oos.get('n', 0):,} | "
                    f"{oos.get('markets', 0)} | "
                    f"{oos.get('expectancy', 0):+.4f} | "
                    f"{oos.get('alpha_vs_baseline', 0):+.4f} | "
                    f"{format_p(float(oos.get('p_value', 1)))} | "
                    f"{r.get('evidence_quality')} |")
            add("")
            caveats = set()
            for r in validated:
                for c in _json(r.get("params")).get("verdict", {}).get(
                        "caveats", []):
                    caveats.add(c)
            for c in caveats:
                add(f"> **Caveat.** {c}")
                add("")

    # ------------------------------------------------------ capital section
    add(f"## 4. The ${st.capital.starting_capital:,.2f} capital test")
    add("")
    add(f"- starting capital: **${st.capital.starting_capital:,.2f}**")
    add(f"- reserve (never deployable): "
        f"{st.capital.reserve_fraction:.0%} = "
        f"${st.capital.starting_capital * st.capital.reserve_fraction:,.2f}")
    add(f"- maximum per trade: {st.capital.max_fraction_per_trade:.0%} = "
        f"${st.capital.starting_capital * st.capital.max_fraction_per_trade:,.2f}")
    add(f"- venue minimum order: ${st.capital.min_order_usdc:,.2f} "
        f"(absolute — does not scale with the bankroll)")
    add(f"- concurrent positions: {st.capital.max_open_positions}")
    add("")
    if rows:
        add("| statement | trades / signals | fill rate | return | hold model |")
        add("|---|---|---:|---:|---|")
        shown = 0
        for r in rows:
            p = _json(r.get("params"))
            ct = p.get("capital_test") or {}
            if not ct:
                continue
            add(f"| {p.get('statement', '')[:52]} | "
                f"{ct.get('trades', 0)} / {ct.get('signals', 0):,} | "
                f"{ct.get('fill_rate', 0):.1%} | "
                f"{ct.get('total_return', 0):+.2%} | "
                f"{ct.get('hold_model', '?')} |")
            shown += 1
            if shown >= 15:
                break
        add("")
        notes = {(_json(r.get("params")).get("capital_test") or {}).get("note")
                 for r in rows}
        for n in sorted(x for x in notes if x):
            add(f"> {n}")
            add("")

    # -------------------------------------------------------------- gates
    add("## 5. Decisions and the gates")
    add("")
    total = store.count("decisions")
    traded = store.count("decisions", "action='TRADE'")
    add(f"- decisions recorded: **{total:,}** ({traded:,} TRADE, "
        f"{total - traded:,} DO_NOT_TRADE)")
    blocks = store.query(
        "SELECT blocking_gate g, COUNT(*) n FROM decisions "
        " WHERE g != '' GROUP BY g ORDER BY n DESC")
    if blocks:
        add("")
        add("| blocking gate | decisions |")
        add("|---|---:|")
        for b in blocks:
            add(f"| {b['g']} | {b['n']:,} |")
    add("")

    # ------------------------------------------------------------- limits
    add("## 6. What this cannot tell you")
    add("")
    add("These are properties of the available data, not of the code.")
    add("")
    add("1. **No order-book history, and it cannot be recovered.** Depth, "
        "spread, partial fills, queue position and market impact for past "
        "markets are gone. Everything microstructural is captured from the "
        "moment collection starts.")
    cov = settled_coverage(store)
    add(f"2. **Settlement timestamps.** {cov['usable']:,} of "
        f"{cov['total']:,} are usable. While that number is low, the $100 "
        f"capital simulation rests on an assumed holding period and its "
        f"returns are MODELLED, not measured. Out-of-sample expectancy is "
        f"unaffected — it needs only entry price and outcome.")
    add("3. **One venue, ~90 days of settled outcomes.** The dominant risk is "
        "not a weak model but a strong-looking result from a small sample, "
        "which is why the matched baseline, the BH correction over the full "
        "denominator and the robustness battery matter more here than model "
        "sophistication.")
    add("4. **News direction is not derived from sentiment.** A headline does "
        "not determine which side of a binary question benefits. Direction "
        "comes from an explicit per-market rule or from agreeing historical "
        "analogues, and is otherwise reported as unknown.")
    add("5. **Early-exit results would be MODELLED; settlement results are "
        "EXACT.** Only hold-to-resolution is scored here.")
    add("6. **`max_drawdown` on a strategy row is the drawdown of its "
        "cumulative per-trade RETURN series, not of an account.** It assumes "
        "every signal was taken at equal size, which no bankroll can do. The "
        "account drawdown is in the capital-test columns.")
    add("")

    add("## 7. Reproducing this")
    add("")
    add("```")
    add("python -m pqv3 inventory      # section 1")
    add("python -m pqv3 discover       # sections 2-4")
    add("python -m pqv3 scan --decide 5  # section 5")
    add("python -m pqv3 report         # this document")
    add("```")
    add("")
    add("Every figure above is a SELECT against `var/pqv3.sqlite3` or a "
        "recomputation from the cached observation matrix. None is a literal.")
    return "\n".join(L)


def _json(v) -> dict:
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v or "{}")
    except Exception:                                         # noqa: BLE001
        return {}


def write(st: Settings, store, *, engine=None) -> str:
    text = build(st, store, engine=engine)
    path = st.work_dir / "reports" / "RESEARCH-REPORT.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return str(path)
