"""A self-contained HTML dashboard, styled to match the existing bot's window.

No dependencies, no build step, no network, no server. It reads whatever
`var/reports/*.json` contains and writes one HTML file that opens in a browser.

VISUAL LANGUAGE. Deliberately the same as `pqb/gui/app.py`, so this reads as
the same product rather than a second one bolted on:

    tabs            Overview · Results · Discovery · Activity · System
    cards           group-box title, 20pt bold value, muted caption, 4 per row
    panels          #f8fafc on #e2e8f0 border, text #1a2733   (light)
                    #1e2530 on #333d4d border, text #d7dde6   (dark)
    status          green #1f9d55 · red #c53030 · amber #b7791f · muted #6b7280
    accent          #2f6fdd

Those chrome values come straight from the existing GUI. The DATA colours are a
separate question and were validated rather than chosen: the categorical slots
carry gate OWNERSHIP, and the set passes lightness, chroma, CVD separation and
normal-vision separation in both modes on the existing dashboard's own panel
surfaces. Light mode sits below 3:1 on three slots, so every bar is directly
labelled and every chart has a table — identity never rests on colour alone.

Reporting rules that survive the restyle:

  * a number is never shown without the denominator that makes it readable
  * the funnel is the hero, because "where do opportunities go" is the whole
    question this project exists to answer
  * sequential encodes magnitude, categorical encodes gate ownership,
    diverging encodes signed quantities — never decoration
"""

from __future__ import annotations

import html
import json
from pathlib import Path

# Categorical slots carry GATE OWNER, in fixed order, never cycled.
OWNER_SLOT = {
    "STRATEGY_A": 1, "STRATEGY_B": 2, "GLOBAL_SAFETY": 3,
    "PORTFOLIO_RISK": 4, "EXECUTION": 5,
}

TABS = ("Overview", "Results", "Discovery", "Activity", "System")

CSS = """
:root{color-scheme:light dark}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{font:13px/1.55 "Segoe UI",ui-sans-serif,system-ui,-apple-system,Roboto,sans-serif}
.app{
  --bg:#eef1f5; --panel:#f8fafc; --border:#e2e8f0; --panel-hd:#eef2f7;
  --ink:#1a2733; --ink-2:#4a5a6b; --muted:#6b7280;
  --green:#1f9d55; --red:#c53030; --amber:#b7791f; --accent:#2f6fdd;
  --s1:#2f6fdd; --s2:#eb6834; --s3:#1baf7a; --s4:#eda100; --s5:#e87ba4;
  --seq-1:#86b6ef; --seq-2:#5598e7; --seq-3:#3987e5; --seq-4:#2f6fdd;
  --seq-5:#256abf; --seq-6:#1c5cab; --seq-7:#184f95; --seq-8:#0d366b;
  --dneg-3:#c62b2a; --dneg-2:#e34948; --dneg-1:#f0a3a2;
  --dpos-1:#9ec5f4; --dpos-2:#5598e7; --dpos-3:#2f6fdd;
  background:var(--bg); color:var(--ink);
  /* Column flex with main growing: on a short tab the status bar is pushed to
     the bottom of the viewport instead of floating mid-page with the app
     background showing underneath it. */
  min-height:100vh; display:flex; flex-direction:column;
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .app{
  --bg:#151a21; --panel:#1e2530; --border:#333d4d; --panel-hd:#232b38;
  --ink:#d7dde6; --ink-2:#aab4c0; --muted:#8b95a3;
  --green:#3fbb77; --red:#e06a6a; --amber:#d9a13a; --accent:#6ea8ff;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
  --seq-1:#184f95; --seq-2:#1c5cab; --seq-3:#256abf; --seq-4:#2a78d6;
  --seq-5:#3987e5; --seq-6:#5598e7; --seq-7:#9ec5f4; --seq-8:#cde2fb;
  --dneg-3:#e06a6a; --dneg-2:#c94b4b; --dneg-1:#8f3130;
  --dpos-1:#1c5cab; --dpos-2:#2f6fdd; --dpos-3:#6ea8ff;
}}
:root[data-theme=dark] .app{
  --bg:#151a21; --panel:#1e2530; --border:#333d4d; --panel-hd:#232b38;
  --ink:#d7dde6; --ink-2:#aab4c0; --muted:#8b95a3;
  --green:#3fbb77; --red:#e06a6a; --amber:#d9a13a; --accent:#6ea8ff;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
  --seq-1:#184f95; --seq-2:#1c5cab; --seq-3:#256abf; --seq-4:#2a78d6;
  --seq-5:#3987e5; --seq-6:#5598e7; --seq-7:#9ec5f4; --seq-8:#cde2fb;
  --dneg-3:#e06a6a; --dneg-2:#c94b4b; --dneg-1:#8f3130;
  --dpos-1:#1c5cab; --dpos-2:#2f6fdd; --dpos-3:#6ea8ff;
}

/* title bar */
.titlebar{background:var(--panel);border-bottom:1px solid var(--border);
  padding:9px 16px;display:flex;align-items:center;gap:12px;position:sticky;top:0;
  z-index:20;flex:0 0 auto}
.titlebar h1{font-size:14px;font-weight:600;margin:0;letter-spacing:.01em}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--border);
  color:var(--muted);background:var(--bg)}
.pill.on{color:#fff;background:var(--green);border-color:transparent}
.pill.off{color:#fff;background:var(--red);border-color:transparent}
.spacer{margin-left:auto}
.btn{background:var(--bg);border:1px solid var(--border);color:var(--ink-2);
  border-radius:4px;padding:4px 11px;cursor:pointer;font-size:12px;font-family:inherit}
.btn:hover{color:var(--ink);border-color:var(--muted)}

/* tab strip, QTabWidget-style */
.tabbar{background:var(--panel-hd);border-bottom:1px solid var(--border);
  padding:0 12px;display:flex;gap:2px;position:sticky;top:39px;z-index:19;
  overflow-x:auto;flex:0 0 auto}
.tab{border:1px solid transparent;border-bottom:none;background:none;cursor:pointer;
  padding:8px 16px;font-size:12.5px;color:var(--muted);font-family:inherit;
  border-radius:4px 4px 0 0;white-space:nowrap}
.tab:hover{color:var(--ink)}
.tab[aria-selected=true]{background:var(--bg);color:var(--ink);font-weight:600;
  border-color:var(--border);margin-bottom:-1px;padding-bottom:9px}

main{padding:16px;max-width:1240px;margin:0 auto;width:100%;flex:1 0 auto}
.page[hidden]{display:none}

/* QGroupBox */
.box{border:1px solid var(--border);border-radius:5px;background:var(--panel);
  margin:0 0 14px;padding:16px 16px 14px;position:relative}
.box>.hd{position:absolute;top:-8px;left:11px;background:var(--panel);
  padding:0 6px;font-size:11.5px;font-weight:600;color:var(--ink-2);
  letter-spacing:.02em}
.note{color:var(--muted);font-size:12px;margin:2px 0 14px;max-width:80ch;line-height:1.6}

/* cards, 4 per row like the Overview grid */
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}}
.card{border:1px solid var(--border);border-radius:5px;background:var(--panel);
  padding:14px 14px 12px;position:relative}
.card>.hd{position:absolute;top:-8px;left:11px;background:var(--panel);padding:0 6px;
  font-size:11px;font-weight:600;color:var(--ink-2)}
.card .v{font-size:20px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums;
  letter-spacing:-.01em}
.card .c{color:var(--muted);font-size:11.5px;margin-top:3px;line-height:1.45}
.green{color:var(--green)}.red{color:var(--red)}.amber{color:var(--amber)}

.explain{background:var(--panel);border:1px solid var(--border);border-radius:5px;
  padding:13px 15px;color:var(--ink-2);font-size:12.5px;line-height:1.65;margin-bottom:14px}
.explain b{color:var(--ink)}

table{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}
th{text-align:left;color:var(--muted);font-weight:600;font-size:11px;
  padding:0 10px 6px 0;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:6px 10px 6px 0;border-bottom:1px solid var(--border);color:var(--ink-2)}
tr:hover td{background:var(--panel-hd)}
td.k{color:var(--ink)}
.num{text-align:right}
.scroll{overflow-x:auto}
.legend{display:flex;gap:15px;flex-wrap:wrap;margin:0 0 12px;font-size:11.5px;color:var(--ink-2)}
.legend i{width:10px;height:10px;border-radius:2px;display:inline-block;margin-right:6px;
  vertical-align:-1px}
/* Cap at the viewBox width so the chart renders 1:1. Letting an SVG scale
   up to fill a wide container magnifies its text and strokes with it, and the
   chart stops matching the surrounding UI. */
svg{display:block;width:100%;max-width:700px;height:auto;overflow:visible}
.bar{transition:opacity .12s}
g.row:hover .bar{opacity:.75}
.vlab{fill:var(--ink);font-size:11px;font-variant-numeric:tabular-nums}
.clab{fill:var(--ink-2);font-size:11px}
.zero{stroke:var(--muted);stroke-width:1;stroke-dasharray:2 2}
.mono{font-family:Consolas,ui-monospace,Menlo,monospace;font-size:11px}
details{margin-top:10px}summary{cursor:pointer;color:var(--accent);font-size:12px}
ul.note{padding-left:18px}ul.note li{margin-bottom:6px}
.statusbar{border-top:1px solid var(--border);background:var(--panel);color:var(--muted);
  font-size:11.5px;padding:10px 16px;line-height:1.7;flex:0 0 auto}
"""

JS = """
(function(){
  var r=document.documentElement,b=document.getElementById('themeBtn');
  function cur(){return r.getAttribute('data-theme')||
    (matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');}
  function set(t){r.setAttribute('data-theme',t);b.textContent=t==='dark'?'Light':'Dark';
    try{localStorage.setItem('pqv2-theme',t);}catch(e){}}
  var saved=null;try{saved=localStorage.getItem('pqv2-theme');}catch(e){}
  set(saved||cur());
  b.addEventListener('click',function(){set(cur()==='dark'?'light':'dark');});

  var tabs=[].slice.call(document.querySelectorAll('.tab'));
  function show(name){
    tabs.forEach(function(t){t.setAttribute('aria-selected',String(t.dataset.tab===name));});
    [].forEach.call(document.querySelectorAll('.page'),function(p){
      p.hidden = p.dataset.tab!==name;});
    try{localStorage.setItem('pqv2-tab',name);}catch(e){}
  }
  tabs.forEach(function(t){t.addEventListener('click',function(){show(t.dataset.tab);});});
  var st=null;try{st=localStorage.getItem('pqv2-tab');}catch(e){}
  show(st && document.querySelector('.page[data-tab="'+st+'"]') ? st : tabs[0].dataset.tab);
})();
"""


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _n(v, spec=",.0f") -> str:
    try:
        return format(float(v), spec)
    except (TypeError, ValueError):
        return "n/a"


# --- primitives -------------------------------------------------------------

def box(title, *body) -> str:
    return (f"<div class='box'><div class='hd'>{_e(title)}</div>"
            + "".join(b for b in body if b) + "</div>")


def card(title, value, caption="", cls="") -> str:
    return (f"<div class='card'><div class='hd'>{_e(title)}</div>"
            f"<div class='v {cls}'>{value}</div>"
            f"<div class='c'>{_e(caption)}</div></div>")


def cards(*items) -> str:
    return f"<div class='cards'>{''.join(items)}</div>"


def hbar(rows, *, width=680, rh=25, gap=6, fmt=",.0f", colors=None,
         label_w=210):
    rows = [r for r in rows if r is not None]
    if not rows:
        return "<p class='note'>no data</p>"
    mx = max((abs(r[1]) for r in rows), default=1) or 1
    plot = width - label_w - 78
    h = len(rows) * (rh + gap)
    out = [f"<svg viewBox='0 0 {width} {h}' role='img'>"]
    for i, row in enumerate(rows):
        lab, val = row[0], row[1]
        col = colors[i] if colors else "var(--seq-4)"
        extra = row[2] if len(row) > 2 else ""
        y = i * (rh + gap)
        w = max(0.0, abs(val) / mx * plot)
        out.append(f"<g class='row'><title>{_e(lab)}: {_n(val, fmt)}"
                   f"{(' — ' + _e(extra)) if extra else ''}</title>"
                   f"<text class='clab' x='0' y='{y + rh * 0.68:.0f}'>{_e(lab)}</text>"
                   f"<rect class='bar' x='{label_w}' y='{y + 4}' width='{w:.1f}' "
                   f"height='{rh - 8}' rx='4' fill='{col}'/>"
                   f"<text class='vlab' x='{label_w + w + 8:.1f}' "
                   f"y='{y + rh * 0.68:.0f}'>{_n(val, fmt)}</text></g>")
    out.append("</svg>")
    return "".join(out)


def diverging_bar(rows, *, width=680, rh=25, gap=6, fmt="+.3f", label_w=120):
    rows = [r for r in rows if r is not None]
    if not rows:
        return "<p class='note'>no data</p>"
    mx = max((abs(v) for _, v, *_ in rows), default=1) or 1
    half = (width - label_w - 96) / 2
    zero = label_w + half
    h = len(rows) * (rh + gap) + 6
    out = [f"<svg viewBox='0 0 {width} {h}' role='img'>",
           f"<line class='zero' x1='{zero}' y1='0' x2='{zero}' y2='{h - 6}'/>"]
    for i, row in enumerate(rows):
        lab, val = row[0], row[1]
        extra = row[2] if len(row) > 2 else ""
        y = i * (rh + gap)
        w = abs(val) / mx * half
        neg = val < 0
        shade = min(3, max(1, int(abs(val) / mx * 3) + 1))
        col = f"var(--{'dneg' if neg else 'dpos'}-{shade})"
        x = zero - w if neg else zero
        tx = (x - 8) if neg else (x + w + 8)
        out.append(f"<g class='row'><title>{_e(lab)}: {_n(val, fmt)}"
                   f"{(' — ' + _e(extra)) if extra else ''}</title>"
                   f"<text class='clab' x='0' y='{y + rh * 0.68:.0f}'>{_e(lab)}</text>"
                   f"<rect class='bar' x='{x:.1f}' y='{y + 4}' width='{max(w, 1):.1f}' "
                   f"height='{rh - 8}' rx='4' fill='{col}'/>"
                   f"<text class='vlab' x='{tx:.1f}' y='{y + rh * 0.68:.0f}' "
                   f"text-anchor='{'end' if neg else 'start'}'>{_n(val, fmt)}</text></g>")
    out.append("</svg>")
    return "".join(out)


def table(headers, rows, aligns=None) -> str:
    aligns = aligns or []
    th = "".join(f"<th class='{'num' if i in aligns else ''}'>{_e(h)}</th>"
                 for i, h in enumerate(headers))
    body = "".join(
        "<tr>" + "".join(
            f"<td class='{'num' if i in aligns else ('k' if i == 0 else '')}'>{c}</td>"
            for i, c in enumerate(r)) + "</tr>" for r in rows)
    return (f"<div class='scroll'><table><thead><tr>{th}</tr></thead>"
            f"<tbody>{body}</tbody></table></div>")


def trunc(s, n=44) -> str:
    """Truncate for the column, keep the whole string in a tooltip.

    A rule cut mid-word with no way to read the rest is a table that hides its
    own content."""
    s = str(s or "")
    short = s if len(s) <= n else s[:n - 1].rstrip() + "…"
    return f"<span class='mono' title='{_e(s)}'>{_e(short)}</span>"


# --- data -------------------------------------------------------------------

def _load(reports: Path) -> dict:
    out = {}
    for name in ("shadow", "last_pass", "strategy_a_audit", "feature_audit",
                 "winners", "reconciliation", "reconciliation_demo", "exits",
                 "expansion", "diagnostic", "rn1", "gate_audit"):
        p = reports / f"{name}.json"
        if p.exists():
            try:
                out[name] = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass
    return out


FUNNEL_STAGES = [("wallet opportunities", "opportunities"),
                 ("signals generated", "received"),
                 ("behaviour matched", "behavior_matched"),
                 ("strategy accepted", "accepted"),
                 ("portfolio approved", "execution_attempted"),
                 ("execution succeeded", "execution_successful")]


def build(reports_dir: Path, *, calibration=None, generated="") -> str:
    d = _load(Path(reports_dir))
    pass_ = d.get("last_pass", {})
    shadow = d.get("shadow", {})
    fn = shadow.get("funnel") or {}
    A, B = fn.get("A") or {}, fn.get("B") or {}
    acct = shadow.get("account") or {}
    sa_all = d.get("strategy_a_audit", {})
    sa = sa_all.get("strategy_a", sa_all) or {}
    orphan = sa_all.get("orphaned_evidence") or {}
    feat = d.get("feature_audit", {})
    win = d.get("winners", {})
    rec = d.get("reconciliation", {})
    demo = d.get("reconciliation_demo", {})
    exits = d.get("exits", {})
    expan = d.get("expansion", {})
    gate_audit = d.get("gate_audit", {})

    hist = dict(pass_.get("status_histogram") or [])
    validated = hist.get("VALIDATED", 0)
    total_cand = sum(hist.values())
    transfer = [a for a in (pass_.get("agreement") or [])
                if a.get("wallets_validated", 0) >= 2]
    dec = sa.get("decisions_total", 0)
    P: dict = {t: [] for t in TABS}

    # ============================ OVERVIEW ============================
    eq = acct.get("equity")
    pnl = acct.get("realized_pnl", 0)
    wins, losses = B.get("wins", 0), B.get("losses", 0)
    P["Overview"].append(cards(
        card("Account value", _n(eq, ",.2f") if eq is not None else "—",
             "simulated, shadow mode"),
        card("Profit / loss", f"{pnl:+,.2f}" if acct else "—",
             "against what you started with",
             "green" if pnl > 0 else ("red" if pnl < 0 else "")),
        card("Open positions", _n(acct.get("open_positions", 0)),
             "trades running now"),
        card("Completed trades", _n(wins + losses),
             f"{wins} winners, {losses} losers"),
        card("Traders watched", _n(len(pass_.get("wallets") or [])),
             "swept this pass"),
        card("Hypotheses tested", _n(pass_.get("hypotheses_tested", 0)),
             "the denominator, always reported"),
        card("Validated", _n(validated), f"of {_n(total_cand)} candidates",
             "green" if validated else "amber"),
        card("Transfers across wallets", _n(len(transfer)),
             "rules validated on 2+ wallets",
             "green" if transfer else "amber"),
    ))
    P["Overview"].append(
        "<div class='explain'><b>The existing engine's zero-trade problem is "
        f"not a filter problem.</b> All {_n(dec)} decisions it has ever "
        "journalled are <span class='mono'>DO_NOTHING</span>, all with one "
        "reason &mdash; <i>learning mode: no validated strategy exists</i>. "
        "That gate sits above every other entry gate, so the market-state, "
        "depth, spread and EV filters were never reached in production. The "
        "measured cause is one step further back: the research pipeline "
        "validates against 3.8 days and 123 markets while 90 days and 1,285 "
        "markets sit in the same database file.</div>")

    ramp = [f"var(--seq-{i})" for i in (1, 2, 3, 4, 6, 8)]
    frows = [(lab, B.get(k, 0)) for lab, k in FUNNEL_STAGES]
    P["Overview"].append(box(
        "Signal funnel — Strategy B",
        "<p class='note'>Where opportunities go, stage by stage. Every signal "
        "leaves in exactly one terminal state carrying the gate that stopped "
        "it, and the arithmetic is asserted to close &mdash; so nothing "
        "disappears unexplained. Later stages carry the stronger colour.</p>",
        hbar(frows, colors=ramp),
        "<details><summary>As a table, with Strategy A beside it</summary>"
        + table(["stage", "Strategy A", "Strategy B"],
                [[_e(l), _n(A.get(k, 0)), _n(B.get(k, 0))]
                 for l, k in FUNNEL_STAGES], aligns=[1, 2])
        + "<p class='note' style='margin-top:10px'>Strategy A is observed "
          "read-only and generates no live signals inside V2, so its column is "
          "structurally zero. It is preserved unmodified and is neither "
          "credited nor blamed.</p></details>"))

    if calibration:
        P["Overview"].append(box(
            "The favourite–longshot bias",
            "<p class='note'>Actual win rate minus the price paid, across all "
            "116,923 settled trades. Favourites win more often than their price "
            "implies and long shots less, so &ldquo;buy between 0.6 and "
            "0.9&rdquo; earns roughly +20% expectancy while copying nobody. "
            "Every candidate is therefore scored against the same price band "
            "and week across all <i>other</i> wallets; zero wallet alpha means "
            "it cannot promote, whatever its profit. <b>This control exists "
            "nowhere in the existing engine.</b></p>",
            diverging_bar([(c["band"], c["gap"], f"n={c['n']:,}")
                           for c in calibration], label_w=100)))

    # ============================ RESULTS ============================
    if win.get("asymmetry"):
        a = win["asymmetry"]
        P["Results"].append(cards(
            card("Win rate", f"{a.get('win_rate', 0):.1%}",
                 f"{_n(a.get('n', 0))} fills"),
            card("Expectancy", f"{a.get('expectancy', 0):+.4f}", "per trade",
                 "green" if a.get("expectancy", 0) > 0 else "red"),
            card("Average win / loss",
                 f"{a.get('avg_win', 0):+.2f} / {a.get('avg_loss', 0):+.2f}",
                 f"W/L ratio {a.get('win_loss_ratio', 0):.2f}"),
            card("Profit factor", f"{a.get('profit_factor', 0):.2f}",
                 "gross win over gross loss",
                 "green" if a.get("profit_factor", 0) > 1 else "red")))
    if win.get("buckets"):
        brows = [(b["bucket"].replace("_", " ").title(), b["n"])
                 for b in win["buckets"] if b["n"]]
        P["Results"].append(box(
            "Winner / loser shape",
            "<p class='note'>The objective is small losses and large winners, "
            "not a high win rate. A 40% win rate at 4:1 beats an 85% win rate "
            "at 1:9, and only expectancy sees the difference.</p>",
            hbar(brows, colors=["var(--seq-4)"] * len(brows), label_w=180),
            (f"<p class='note' style='margin-top:12px'><b>Interpretation.</b> "
             f"{_e(win.get('note'))}</p>" if win.get("note") else "")))
    if exits.get("rows"):
        rows = [[_e(r["exit"]), _n(r["n_filled"]), f"{r['expectancy']:+.4f}",
                 f"{r['win_rate']:.1%}", f"{r['profit_factor']:.2f}",
                 f"{r['win_loss_ratio']:.2f}",
                 ("exact" if r["confidence"] == "exact"
                  else "<span class='amber'>modelled</span>")]
                for r in exits["rows"][:10]]
        P["Results"].append(box(
            "Settlement versus early exit",
            "<p class='note'>No generic exit is imposed; each is measured. "
            "Settlement payoffs are <b>exact</b>; every other row is "
            "<b>modelled</b> off tape prints, so the two are not directly "
            "comparable and a modelled result does not unseat an exact one on "
            "a thin margin.</p>",
            table(["exit", "fills", "expectancy", "win rate", "profit factor",
                   "W/L", "confidence"], rows, aligns=[1, 2, 3, 4, 5]),
            f"<p class='note' style='margin-top:12px'><b>Verdict.</b> "
            f"{_e(exits.get('verdict'))}</p>" if exits.get("verdict") else ""))
    if expan.get("rows"):
        rows = [[f"{r['multiplier']:.2f}x", _n(r["pnl"], ",.0f"),
                 f"{r['roi']:+.4f}", _n(r["max_drawdown"], ",.0f"),
                 f"{(r['pnl'] / (r['max_drawdown'] or 1)):.2f}",
                 f"{r['tail_loss_p05']:+.4f}"] for r in expan["rows"]]
        P["Results"].append(box(
            "Win Expansion ladder",
            "<p class='note'>The multiplier is <i>discovered</i>, never "
            "assumed &mdash; 1.5x is not privileged. Recommended on return per "
            "unit of drawdown, because the step that makes the most money is "
            "almost always the largest one, and the step that makes the most "
            "money per unit of pain usually is not.</p>",
            table(["multiplier", "P&L", "ROI", "max drawdown", "P&L / drawdown",
                   "tail loss p05"], rows, aligns=[1, 2, 3, 4, 5]),
            f"<p class='note' style='margin-top:12px'>{_e(expan.get('note'))}</p>"))
    if not P["Results"]:
        P["Results"].append(box("Results", "<p class='note'>Nothing yet. Run "
                                "<span class='mono'>discover</span> and "
                                "<span class='mono'>shadow</span> first.</p>"))

    # ============================ DISCOVERY ============================
    order = ["INSUFFICIENT_EVIDENCE", "UNPRICEABLE", "FAILED", "NOT_SIGNIFICANT",
             "NO_WALLET_ALPHA", "CONCENTRATED", "UNSTABLE", "FRAGILE", "DRIFT",
             "VALIDATED"]
    srows = [(s, hist[s]) for s in order if hist.get(s)]
    if srows:
        cols = ["var(--seq-3)" if s != "VALIDATED" else "var(--green)"
                for s, _ in srows]
        P["Discovery"].append(box(
            "Where candidates stopped",
            "<p class='note'>The bar a candidate fails at IS the finding &mdash; "
            "it names the next experiment. Reading this histogram tells you "
            "what to fix; reading only the validated count does not.</p>",
            hbar(srows, colors=cols, label_w=210)))
    agree = pass_.get("agreement") or []
    if agree:
        P["Discovery"].append(box(
            "Cross-wallet transfer",
            "<p class='note'>The same rule, tested on different people. This is "
            "the only evidence here that is not vulnerable to having picked the "
            "wallet first &mdash; one wallet with a good record is a sample of "
            "one. Judge it on <i>validated on</i> and the cross-wallet "
            "t-statistic, not on mean expectancy.</p>",
            table(["rule", "validated on", "positive", "mean alpha", "x-wallet t"],
                  [[trunc(a["describe"], 46),
                    _n(a["wallets_validated"]),
                    f"{a['wallets_positive']}/{a['wallets_tested']}",
                    f"{a['mean_alpha']:+.4f}",
                    f"{a.get('cross_wallet_t', 0):.2f}"] for a in agree[:8]],
                  aligns=[1, 2, 3, 4])))
    vals = pass_.get("validated") or []
    if vals:
        P["Discovery"].append(box(
            "Top validated strategies",
            "<p class='note'>Ranked by a multi-factor score, never by P&amp;L: "
            "sample size saturates, and concentration, instability and drawdown "
            "subtract. <b>VALIDATED authorises paper trading only.</b> Note the "
            "market counts &mdash; an edge measured over 9&ndash;12 markets in a "
            "27-day window is a lead, not a licence.</p>",
            table(["score", "wallet", "OOS exp", "wallet alpha", "fills",
                   "markets", "rule"],
                  [[f"{v['score']:.3f}",
                    f"<span class='mono'>{_e(v['wallet'][:12])}</span>",
                    f"{v['oos'].get('expectancy', 0):+.4f}",
                    f"{v['alpha'].get('alpha', 0):+.4f}",
                    _n(v["oos"].get("n_filled", 0)),
                    _n(v["oos"].get("n_markets", 0)),
                    trunc(v["describe"], 40)] for v in vals[:12]],
                  aligns=[0, 2, 3, 4, 5])))
    base = pass_.get("baselines") or []
    if base:
        P["Discovery"].append(box(
            "Naive-copy baselines",
            "<p class='note'>What copying each wallet earns out-of-sample with "
            "no conditioning at all. A candidate that cannot beat this is not a "
            "strategy, it is a wallet. The last column is the share of trades "
            "that had any settled track record behind them when placed.</p>",
            table(["wallet", "OOS fills", "expectancy", "fill rate",
                   "point-in-time evidence"],
                  [[f"<span class='mono'>{_e(b['wallet'][:12])}</span>",
                    _n(b["naive_oos_fills"]),
                    f"<span class='{'green' if b['naive_oos_expectancy'] > 0 else 'red'}'>"
                    f"{b['naive_oos_expectancy']:+.4f}</span>",
                    f"{b['naive_fill_rate']:.0%}",
                    f"{b.get('pit_evidence_share', 0):.0%}"] for b in base[:16]],
                  aligns=[1, 2, 3, 4])))
    if feat.get("features"):
        P["Discovery"].append(box(
            "Feature audit",
            f"<p class='note'>{_e(feat.get('note'))}</p>",
            table(["feature", "distinct values", "status"],
                  [[f"<span class='mono'>{_e(f['name'])}</span>",
                    _n(f["distinct"]),
                    "<span class='amber'>INERT</span>" if f["inert"]
                    else f"lift {f['lift']:+.4f}"]
                   for f in feat["features"][:16]], aligns=[1])))
    if not P["Discovery"]:
        P["Discovery"].append(box("Discovery", "<p class='note'>No pass yet. "
                                  "Run <span class='mono'>discover -v</span>.</p>"))

    # ============================ ACTIVITY ============================
    rej = B.get("top_rejections") or []
    if rej:
        try:
            from ..gates import REGISTRY
            owner_of = {k: g.owner.value for k, g in REGISTRY.items()}
        except Exception:                                    # noqa: BLE001
            owner_of = {}
        rrows, rcols, seen = [], [], []
        for gate, n in rej[:10]:
            o = owner_of.get(gate, "UNREGISTERED")
            rrows.append((gate, n, o))
            rcols.append(f"var(--s{OWNER_SLOT.get(o, 1)})")
            if o not in seen:
                seen.append(o)
        leg = "".join(f"<span><i style='background:var(--s"
                      f"{OWNER_SLOT.get(o, 1)})'></i>{_e(o)}</span>"
                      for o in seen)
        P["Activity"].append(box(
            "What suppressed the most opportunities",
            "<p class='note'>Coloured by which layer <b>owns</b> the rule. Only "
            "GLOBAL_SAFETY and PORTFOLIO_RISK may bind both routes, and every "
            "global gate must carry written evidence &mdash; a global gate "
            "without evidence is a Strategy A gate in disguise. A portfolio "
            "rejection is recorded separately from a strategy rejection so that "
            "&ldquo;is this strategy good?&rdquo; and &ldquo;can we afford "
            "it?&rdquo; stay different questions.</p>",
            f"<div class='legend'>{leg}</div>",
            hbar(rrows, colors=rcols, label_w=225)))
    if rec or demo:
        before = rec.get("before") or {}
        P["Activity"].append(box(
            "Reconciliation exit safety",
            "<p class='note'>A data disagreement is not a trading decision. The "
            "existing engine closes a position on a single absence from one "
            "snapshot, at last known mark, and that closure then feeds its "
            "empirical entry gate as though a strategy had decided it.</p>",
            cards(
                card("Events recorded", _n(before.get("reconciliation_rows", 0)),
                     "in the existing journal"),
                card("Lifecycles", _n(before.get("lifecycles_total", 0)),
                     "positions ever opened"),
                card("Would have closed",
                     _n((demo or {}).get("would_have_exited_before", 0)),
                     "old path, on one empty snapshot", "red"),
                card("Prevented", _n((demo or {}).get("exits_prevented", 0)),
                     "patched path", "green")),
            "<p class='note'><b>The defect is latent, not observed.</b> The "
            "engine has never opened a position, so this path has never run in "
            "production. The patch is preventive &mdash; no P&amp;L was lost "
            "and none is claimed to have been recovered.</p>"))
    if not P["Activity"]:
        P["Activity"].append(box("Activity", "<p class='note'>No signals yet. "
                                 "Run <span class='mono'>shadow</span>.</p>"))

    # ============================ SYSTEM ============================
    P["System"].append(cards(
        card("Strategy A decisions", _n(dec), "every one DO_NOTHING",
             "red" if dec and not sa.get("executions") else ""),
        card("Strategy A trades", _n(sa.get("executions", 0)),
             f"of {_n(dec)} decisions, every one DO_NOTHING",
             "red" if not sa.get("executions") else ""),
        card("Blocking gate", f"<span class='mono' style='font-size:13px'>"
             f"{_e(sa.get('blocking_gate') or '—')}</span>",
             "above every other entry gate"),
        card("Orphaned validated", _n(len(orphan.get("validated") or [])),
             "exist, but nothing reads them",
             "amber" if orphan.get("validated") else "")))
    if sa.get("library_statuses"):
        P["System"].append(box(
            "Strategy A — preserved, unmodified",
            "<p class='note'>Read-only. V2 opens the existing databases with "
            "<span class='mono'>mode=ro</span> and writes only under "
            "<span class='mono'>var/</span>. Strategy A has never executed a "
            "trade, so there is no evidence either way: it is neither marked "
            "DISABLED (no evidence of harm) nor PRODUCTION (no evidence of "
            "edge).</p>",
            table(["library status", "strategies"],
                  [[_e(s), _n(n)] for s, n in sa["library_statuses"]],
                  aligns=[1]),
            f"<p class='note' style='margin-top:12px'>{_e(sa.get('verdict'))}</p>"))
    if orphan.get("validated"):
        P["System"].append(box(
            "Validated strategies that nothing reads",
            "<p class='note'><span class='mono'>wallet-strategy-lab</span> has "
            "validated these. The trading engine reads a different database and "
            "there is no reference to <span class='mono'>walletlab</span> "
            "anywhere in it &mdash; so the account sat parked &ldquo;until "
            "discovery produces a validated strategy&rdquo; while discovery had "
            "already produced two.</p>",
            table(["wallet", "score", "OOS p", "price band", "test exp",
                   "fills / markets", "win rate"],
                  [[f"<span class='mono'>{_e(v['wallet'][:14])}</span>",
                    f"{v['score']}", f"{v['oos_p']:.1e}",
                    f"{v['price_band'][0]}&ndash;{v['price_band'][1]}",
                    f"{v['test_expectancy']:+.4f}",
                    f"{v['test_fills']} / {v['test_markets']}",
                    f"{v['test_win_rate']}"] for v in orphan["validated"]],
                  aligns=[1, 2, 4, 5, 6]),
            "<p class='note' style='margin-top:12px'><b>Do not connect these "
            "yet.</b></p><ul class='note'>"
            + "".join(f"<li>{_e(c)}</li>" for c in orphan.get("caveats") or [])
            + "</ul>"))
    if gate_audit.get("by_owner"):
        P["System"].append(box(
            "Gate ownership",
            "<p class='note'>Every rule that can stop a trade declares an owner. "
            "A Strategy A gate evaluated on the Strategy B route raises &mdash; "
            "that is rule 4 made mechanical rather than promised.</p>",
            table(["owner", "gates"],
                  [[_e(o), f"<span class='mono'>{_e(', '.join(ks))}</span>"]
                   for o, ks in sorted(gate_audit["by_owner"].items())])))

    # --- assemble -----------------------------------------------------------
    tabbar = "".join(
        f"<button class='tab' data-tab='{t}' role='tab' "
        f"aria-selected='false'>{t}</button>" for t in TABS)
    pages = "".join(
        f"<div class='page' data-tab='{t}' role='tabpanel' hidden>"
        f"{''.join(P[t])}</div>" for t in TABS)
    live = ("<span class='pill off'>NOT TRADING</span>"
            if not sa.get("executions") else "<span class='pill on'>LIVE</span>")

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Quant Bridge — V2 Dashboard</title>
<style>{CSS}</style></head>
<body class="app">
<div class="titlebar">
  <h1>Polymarket Quant Bridge <span style="color:var(--muted);font-weight:400">— V2 research</span></h1>
  {live}
  <span class="pill">shadow mode</span>
  <span class="spacer"></span>
  <span class="pill">{_e(generated)}</span>
  <button class="btn" id="themeBtn">Dark</button>
</div>
<div class="tabbar" role="tablist">{tabbar}</div>
<main>{pages}</main>
<div class="statusbar">
Generated from <span class="mono">var/reports/*.json</span>. Nothing here has traded real money. <b>VALIDATED</b> means survived historical out-of-sample
validation and authorises paper trading only; going live is a human decision
this system never makes. No claim of guaranteed profit is made, and none should
be inferred from any number on this page.
The original installation was not modified &mdash; verify with
<span class="mono">git status Polymarket-Bot-DAVID/</span>.
</div>
<script>{JS}</script></body></html>"""


def write(reports_dir: Path, out_path: Path, *, calibration=None,
          generated="") -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build(reports_dir, calibration=calibration,
                              generated=generated), encoding="utf-8")
    return out_path
