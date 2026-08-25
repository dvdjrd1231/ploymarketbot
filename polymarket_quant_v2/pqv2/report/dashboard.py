"""The dashboard: every field the brief lists, rendered for a terminal.

Design rule throughout: a number is never shown without the denominator that
makes it interpretable. "68% win rate" is not information; "68% win rate over
31 fills in 4 markets, win/loss ratio 0.4" is. The V1 system's most expensive
habit was reporting impressive-looking counts -- traders analysed, rules
discovered, learning snapshots -- that no decision depended on.

Rendering is plain text with no dependencies so it works over SSH, in a log
file, and in the client's zip without a build step.
"""

from __future__ import annotations

import json


def _bar(value: float, width: int = 24, lo: float = 0.0, hi: float = 1.0) -> str:
    if hi <= lo:
        return " " * width
    f = max(0.0, min(1.0, (value - lo) / (hi - lo)))
    n = int(f * width)
    return "#" * n + "." * (width - n)


def _fmt(v, spec: str = "") -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return format(v, spec or ".4f")
    if isinstance(v, int):
        return format(v, spec or ",d")
    return str(v)


def _section(title: str) -> str:
    return f"\n{title}\n{'-' * len(title)}"


def render(*, pass_report=None, funnel=None, account=None, strategy_a=None,
           gate_audit=None, correlations=None, expansion=None,
           feature_audit=None, accel=None, leaderboard=None) -> str:
    L: list = []
    pr = pass_report or {}
    fn = funnel or {}
    A = fn.get("A") or {}
    B = fn.get("B") or {}
    acct = account or {}

    L.append("=" * 74)
    L.append("POLYMARKET QUANT ENGINE V2 - DASHBOARD")
    L.append("=" * 74)

    # --- the funnel, both routes side by side ---------------------------
    L.append(_section("SIGNAL FUNNEL  (A = existing engine, B = wallet/RN1 engine)"))
    L.append(f"{'stage':<28}{'STRATEGY A':>16}{'STRATEGY B':>16}")
    rows = [
        ("wallet opportunities", "opportunities"),
        ("signals generated", "received"),
        ("behaviour matched", "behavior_matched"),
        ("strategy accepted", "accepted"),
        ("strategy rejected", "strategy_rejected"),
        ("risk rejected", "risk_rejected"),
        ("portfolio rejected", "portfolio_rejected"),
        ("execution attempted", "execution_attempted"),
        ("execution succeeded", "execution_successful"),
        ("execution failed", "execution_failed"),
        ("wins", "wins"),
        ("losses", "losses"),
    ]
    for label, key in rows:
        L.append(f"{label:<28}{_fmt(A.get(key, 0)):>16}{_fmt(B.get(key, 0)):>16}")
    L.append(f"{'P&L':<28}{_fmt(A.get('pnl', 0.0), ',.2f'):>16}"
             f"{_fmt(B.get('pnl', 0.0), ',.2f'):>16}")
    L.append(f"{'expectancy':<28}{_fmt(A.get('expectancy', 0.0), '+.5f'):>16}"
             f"{_fmt(B.get('expectancy', 0.0), '+.5f'):>16}")
    L.append(f"{'win rate':<28}{_rate(A):>16}{_rate(B):>16}")
    L.append(f"{'average win':<28}{_fmt(A.get('avg_win', 0.0), '+.4f'):>16}"
             f"{_fmt(B.get('avg_win', 0.0), '+.4f'):>16}")
    L.append(f"{'average loss':<28}{_fmt(A.get('avg_loss', 0.0), '+.4f'):>16}"
             f"{_fmt(B.get('avg_loss', 0.0), '+.4f'):>16}")

    for route, data in (("A", A), ("B", B)):
        if data and not data.get("balanced", True):
            L.append(f"  !! route {route} funnel does not reconcile: "
                     "there is an unexplained gap between DATA and TRADE")

    # --- rejections ------------------------------------------------------
    L.append(_section("REJECTION REASONS  (which rules suppress the most)"))
    any_rej = False
    for route, data in (("A", A), ("B", B)):
        for gate, n in (data.get("top_rejections") or [])[:8]:
            any_rej = True
            L.append(f"  [{route}] {gate:<28} {n:>8,}  {_owner(gate)}")
    if not any_rej:
        L.append("  (no rejections recorded this cycle)")

    # --- account ---------------------------------------------------------
    if acct:
        L.append(_section("ACCOUNT / COMPOUNDING"))
        L.append(f"  starting capital  {_fmt(acct.get('starting_capital'), ',.2f'):>14}")
        L.append(f"  current equity    {_fmt(acct.get('equity'), ',.2f'):>14}")
        L.append(f"  realized P&L      {_fmt(acct.get('realized_pnl'), '+,.2f'):>14}")
        L.append(f"  compounded return {_fmt(acct.get('compounded_return'), '+.2%'):>14}")
        L.append(f"  profit factor     {_fmt(acct.get('profit_factor'), '.3f'):>14}")
        L.append(f"  open positions    {_fmt(acct.get('open_positions')):>14}")
        L.append(f"  deployable        {_fmt(acct.get('deployable'), ',.2f'):>14}")
        L.append(f"  exposure          {_fmt(acct.get('exposure'), '.1%'):>14}")
        dd = acct.get("max_drawdown", 0.0)
        L.append(f"  max drawdown      {_fmt(dd, '.2%'):>14}  {_bar(dd, 20, 0, 0.5)}")
        if acct.get("halted"):
            L.append(f"  !! HALTED: {acct.get('halt_reason')}")

    # --- discovery -------------------------------------------------------
    if pr:
        L.append(_section("DISCOVERY PASS"))
        L.append(f"  wallets swept        {len(pr.get('wallets') or []):>10,}")
        L.append(f"  hypotheses tested    {pr.get('hypotheses_tested', 0):>10,}")
        L.append(f"  selection penalty    {pr.get('selection_penalty', 0):>10,}"
                 "   (implicit hypotheses spent choosing the reference wallet)")
        L.append(f"  BH threshold         {pr.get('bh_threshold', 0.0):>10.5f}"
                 f"   ({pr.get('bh_significant', 0):,} significant)")
        L.append(f"  elapsed              {pr.get('seconds', 0):>10.1f}s")
        L.append("")
        L.append("  status histogram (WHERE candidates stop is the finding):")
        hist = pr.get("status_histogram") or []
        total = sum(n for _, n in hist) or 1
        for status, n in hist:
            L.append(f"    {status:<24}{n:>7,}  {n / total:>6.1%}  "
                     f"{_bar(n / total, 18)}")

        base = pr.get("baselines") or []
        if base:
            L.append("")
            L.append("  naive-copy baselines (what copying the wallet earns "
                     "with no conditioning):")
            L.append(f"    {'wallet':<16}{'oos n':>8}{'expectancy':>13}"
                     f"{'fill rate':>11}{'pit evid':>10}")
            for r in base[:12]:
                L.append(f"    {r['wallet'][:14]:<16}{r['naive_oos_fills']:>8,}"
                         f"{r['naive_oos_expectancy']:>+13.4f}"
                         f"{r['naive_fill_rate']:>11.1%}"
                         f"{r.get('pit_evidence_share', 0):>10.1%}")

    # --- leaderboard -----------------------------------------------------
    if leaderboard:
        L.append(_section("TOP STRATEGIES  (multi-factor score, not P&L)"))
        L.append(f"  {'score':>6} {'status':<22}{'wallet':<14}{'exp':>9}"
                 f"{'alpha':>9}{'n':>7}{'mkts':>6}")
        for row in leaderboard[:15]:
            oos = json.loads(row.get("oos") or "{}") if isinstance(
                row.get("oos"), str) else (row.get("oos") or {})
            alpha = json.loads(row.get("alpha") or "{}") if isinstance(
                row.get("alpha"), str) else (row.get("alpha") or {})
            L.append(f"  {row.get('score', 0):>6.3f} "
                     f"{row.get('status', ''):<22}"
                     f"{(row.get('wallet') or '')[:12]:<14}"
                     f"{oos.get('expectancy', 0):>+9.4f}"
                     f"{alpha.get('alpha', 0):>+9.4f}"
                     f"{oos.get('n_filled', 0):>7,}"
                     f"{oos.get('n_markets', 0):>6}")

    # --- families / transferability --------------------------------------
    fams = pr.get("families") or []
    agree = pr.get("agreement") or []
    L.append(_section("STRATEGY FAMILIES / CROSS-WALLET TRANSFER"))
    if fams:
        for f in fams[:6]:
            L.append(f"  {f['family_id']}  {f['size']} wallets  "
                     f"cohesion {f['cohesion']:.2f}  "
                     f"independent support {f['independent_support']}")
            if f.get("note"):
                L.append(f"      note: {f['note']}")
    else:
        L.append("  no behavioural clusters met the cohesion bar")
    if agree:
        L.append("")
        L.append("  rules that appear on more than one wallet:")
        L.append(f"    {'validated':>10}{'positive':>10}{'tested':>8}"
                 f"{'mean alpha':>12}{'x-wallet t':>12}  rule")
        for r in agree[:8]:
            L.append(f"    {r['wallets_validated']:>10}{r['wallets_positive']:>10}"
                     f"{r['wallets_tested']:>8}{r['mean_alpha']:>+12.4f}"
                     f"{r.get('cross_wallet_t', 0):>12.2f}  {r['describe'][:40]}")
    else:
        L.append("  no rule was positive on two or more wallets - nothing has "
                 "been shown to transfer")

    # --- sizing ----------------------------------------------------------
    if expansion:
        L.append(_section("WIN EXPANSION"))
        L.append(f"  {'multiplier':>11}{'pnl':>12}{'roi':>10}{'max dd':>11}"
                 f"{'pnl/dd':>9}{'tail p05':>11}")
        for r in expansion.get("rows", []):
            L.append(f"  {r['multiplier']:>11.2f}{r['pnl']:>12,.0f}"
                     f"{r['roi']:>+10.4f}{r['max_drawdown']:>11,.0f}"
                     f"{(r['pnl'] / (r['max_drawdown'] or 1)):>9.2f}"
                     f"{r['tail_loss_p05']:>+11.4f}")
        L.append(f"  recommended: {expansion.get('recommended', 1.0):.2f}x")
        L.append(f"  {expansion.get('note', '')}")

    # --- correlation -----------------------------------------------------
    if correlations:
        L.append(_section("STRATEGY CORRELATION  (two strategies at 0.9 are one bet)"))
        for c in correlations[:8]:
            L.append(f"  {c['correlation']:>+7.3f}  n={c['n']:<6} "
                     f"{c['a'][:24]} <-> {c['b'][:24]}")

    # --- features --------------------------------------------------------
    if feature_audit:
        L.append(_section("FEATURE AUDIT"))
        L.append(f"  {feature_audit.get('note', '')}")
        if feature_audit.get("inert_features"):
            L.append(f"  inert: {', '.join(feature_audit['inert_features'])}")
        live = [f for f in feature_audit.get("features", [])
                if not f["inert"] and f["p_value"] < 0.05][:8]
        if live:
            L.append("  features carrying information (uncorrected p-values):")
            for f in live:
                L.append(f"    {f['name']:<24} lift {f['lift']:>+9.4f}  "
                         f"t {f['t_stat']:>7.2f}  p {f['p_value']:.5f}")

    # --- strategy A ------------------------------------------------------
    if strategy_a:
        L.append(_section("STRATEGY A  (existing engine - preserved, unmodified)"))
        L.append(f"  decisions {strategy_a.get('decisions_total', 0):,}   "
                 f"executions {strategy_a.get('executions', 0):,}   "
                 f"lifecycles {strategy_a.get('lifecycles', 0):,}")
        if strategy_a.get("library_statuses"):
            L.append("  library: " + ", ".join(
                f"{s}={n}" for s, n in strategy_a["library_statuses"]))
        if strategy_a.get("blocking_gate"):
            L.append(f"  blocking gate: {strategy_a['blocking_gate']}")
        for line in _wrap(strategy_a.get("verdict", ""), 70):
            L.append(f"  {line}")

    # --- accel -----------------------------------------------------------
    if accel:
        L.append(_section("ACCELERATION"))
        L.append(f"  mode={accel.get('mode')}  "
                 f"rust_available={accel.get('rust_available')}  "
                 f"effective={accel.get('effective_backend')}")
        if accel.get("unavailable_reason"):
            L.append(f"  {accel['unavailable_reason']}")
        for k, v in (accel.get("divergences") or {}).items():
            flag = "OK" if v["equivalent"] else "DIVERGED"
            L.append(f"  shadow {k}: {flag} ({v['diverged']}/{v['checked']}, "
                     f"worst {v['worst_abs_diff']:g})")

    L.append("")
    L.append("=" * 74)
    return "\n".join(L)


def _rate(d: dict) -> str:
    w, l = d.get("wins", 0), d.get("losses", 0)
    return f"{w / (w + l):.1%}" if (w + l) else "n/a"


def _owner(gate_key: str) -> str:
    from ..gates import REGISTRY
    g = REGISTRY.get(gate_key)
    return f"[{g.owner.value}]" if g else "[UNREGISTERED]"


def _wrap(text: str, width: int) -> list:
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
