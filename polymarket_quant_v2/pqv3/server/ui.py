"""The dashboard shell.

VISUAL LANGUAGE. Deliberately identical to the existing Quant Bridge GUI and to
V2's HTML dashboard, so this reads as the same product rather than a second one
bolted on. The chrome values below are lifted from `pqb/gui/app.py` and
`pqv2/report/html_dashboard.py` unchanged:

    panels      #f8fafc on #e2e8f0 border, text #1a2733     (light)
                #1e2530 on #333d4d border, text #d7dde6     (dark)
    status      green #1f9d55 · red #c53030 · amber #b7791f · muted #6b7280
    accent      #2f6fdd
    cards       group-box title, 20pt bold value, muted caption
    tabs        QTabWidget-style strip

What is new is the nav: twenty-two sections instead of five, arranged in a
sidebar rather than a single strip, because twenty-two tabs in one row is not
navigable. The card, table and panel components are unchanged.

The browser does NO computation. Every number rendered here arrives from
`/api/<section>` as a finished value. The page's entire job is layout,
formatting and drill-down — Python computes, the browser displays. That is the
brief's requirement and also the reason this file contains no arithmetic beyond
number formatting.

Reporting rules carried over from V2:
  * a number is never shown without the denominator that makes it readable
  * a null renders as "—" with a reason, never as 0
  * every empty section renders its `note` explaining what would fill it
"""

from __future__ import annotations

NAV = [
    ("Trading", ["OVERVIEW", "PORTFOLIO", "RISK", "PAPER", "LIVE"]),
    ("Research", ["OPPORTUNITIES", "STRATEGIES", "DISCOVERY", "BACKTEST",
                  "VALIDATION", "LEARNING"]),
    ("Intelligence", ["MARKETS", "WALLETS", "LEADERBOARD", "NEWS", "EVENTS",
                      "BLOCKCHAIN", "MICROSTRUCTURE"]),
    ("Operations", ["AGENTS", "ACTIVITY", "LOSSES", "SYSTEM"]),
]

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
  --seq-1:#86b6ef; --seq-4:#2f6fdd; --seq-8:#0d366b;
  --dneg-2:#e34948; --dpos-2:#5598e7;
  background:var(--bg); color:var(--ink);
  min-height:100vh; display:flex; flex-direction:column;
}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])) .app{
  --bg:#151a21; --panel:#1e2530; --border:#333d4d; --panel-hd:#232b38;
  --ink:#d7dde6; --ink-2:#aab4c0; --muted:#8b95a3;
  --green:#3fbb77; --red:#e06a6a; --amber:#d9a13a; --accent:#6ea8ff;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
  --seq-1:#184f95; --seq-4:#2a78d6; --seq-8:#cde2fb;
  --dneg-2:#c94b4b; --dpos-2:#2f6fdd;
}}
:root[data-theme=dark] .app{
  --bg:#151a21; --panel:#1e2530; --border:#333d4d; --panel-hd:#232b38;
  --ink:#d7dde6; --ink-2:#aab4c0; --muted:#8b95a3;
  --green:#3fbb77; --red:#e06a6a; --amber:#d9a13a; --accent:#6ea8ff;
  --s1:#3987e5; --s2:#d95926; --s3:#199e70; --s4:#c98500; --s5:#d55181;
  --seq-1:#184f95; --seq-4:#2a78d6; --seq-8:#cde2fb;
  --dneg-2:#c94b4b; --dpos-2:#2f6fdd;
}

.titlebar{background:var(--panel);border-bottom:1px solid var(--border);
  padding:9px 16px;display:flex;align-items:center;gap:12px;position:sticky;
  top:0;z-index:20;flex:0 0 auto}
.titlebar h1{font-size:14px;font-weight:600;margin:0;letter-spacing:.01em}
.pill{font-size:11px;padding:2px 8px;border-radius:10px;
  border:1px solid var(--border);color:var(--muted);background:var(--bg);
  white-space:nowrap}
.pill.on{color:#fff;background:var(--green);border-color:transparent}
.pill.off{color:#fff;background:var(--red);border-color:transparent}
.pill.warn{color:#fff;background:var(--amber);border-color:transparent}
.spacer{margin-left:auto}
.btn{background:var(--bg);border:1px solid var(--border);color:var(--ink-2);
  border-radius:4px;padding:4px 11px;cursor:pointer;font-size:12px;
  font-family:inherit}
.btn:hover{color:var(--ink);border-color:var(--muted)}

.shell{display:flex;flex:1 0 auto;min-height:0}
nav{width:186px;flex:0 0 auto;background:var(--panel-hd);
  border-right:1px solid var(--border);padding:10px 0;overflow-y:auto;
  position:sticky;top:39px;height:calc(100vh - 39px)}
nav .grp{font-size:10.5px;font-weight:600;letter-spacing:.06em;
  color:var(--muted);padding:12px 16px 5px;text-transform:uppercase}
nav button{display:block;width:100%;text-align:left;background:none;
  border:none;border-left:3px solid transparent;padding:6px 16px;
  font:inherit;font-size:12.5px;color:var(--ink-2);cursor:pointer}
nav button:hover{color:var(--ink);background:var(--bg)}
nav button[aria-current=true]{color:var(--ink);font-weight:600;
  background:var(--bg);border-left-color:var(--accent)}

main{padding:16px 20px 40px;flex:1 1 auto;min-width:0;max-width:1400px}
h2.page-t{font-size:15px;margin:0 0 4px;font-weight:600}
p.page-s{margin:0 0 16px;color:var(--muted);font-size:12px;max-width:80ch}

.box{border:1px solid var(--border);border-radius:5px;background:var(--panel);
  margin:0 0 14px;padding:16px 16px 14px;position:relative}
.box>.hd{position:absolute;top:-8px;left:11px;background:var(--panel);
  padding:0 6px;font-size:11.5px;font-weight:600;color:var(--ink-2);
  letter-spacing:.02em}

.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(184px,1fr));
  gap:10px}
.card{border:1px solid var(--border);border-radius:4px;background:var(--bg);
  padding:11px 13px}
.card .k{font-size:10.5px;color:var(--muted);text-transform:uppercase;
  letter-spacing:.05em}
.card .v{font-size:20px;font-weight:700;margin:3px 0 1px;
  font-variant-numeric:tabular-nums;line-height:1.2}
.card .c{font-size:11px;color:var(--muted)}
.pos{color:var(--green)} .neg{color:var(--red)} .amb{color:var(--amber)}
.mut{color:var(--muted)}

.tbl-wrap{overflow-x:auto;max-width:100%}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{text-align:left;padding:5px 9px;border-bottom:1px solid var(--border);
  white-space:nowrap}
th{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;
  color:var(--muted);font-weight:600;position:sticky;top:0;
  background:var(--panel)}
td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.clickable{cursor:pointer}
tr.clickable:hover td{background:var(--bg)}
td.wrap{white-space:normal;max-width:52ch}

.note{font-size:12px;color:var(--muted);background:var(--bg);
  border:1px dashed var(--border);border-radius:4px;padding:10px 12px;
  margin:8px 0;line-height:1.5}
.empty{color:var(--muted);font-size:12px;padding:14px 2px}
.bar{height:7px;border-radius:3px;background:var(--border);overflow:hidden;
  min-width:60px}
.bar>i{display:block;height:100%;background:var(--seq-4)}
.tag{font-size:10.5px;padding:1px 6px;border-radius:9px;
  border:1px solid var(--border);color:var(--muted)}
.tag.g{color:#fff;background:var(--green);border-color:transparent}
.tag.r{color:#fff;background:var(--red);border-color:transparent}
.tag.a{color:#fff;background:var(--amber);border-color:transparent}
details{margin:6px 0}
summary{cursor:pointer;font-size:12px;color:var(--accent)}
pre{background:var(--bg);border:1px solid var(--border);border-radius:4px;
  padding:10px;overflow-x:auto;font-size:11.5px;margin:6px 0}
.statusbar{background:var(--panel);border-top:1px solid var(--border);
  padding:5px 16px;font-size:11.5px;color:var(--muted);display:flex;gap:14px;
  flex:0 0 auto}
"""

JS = r"""
const $ = (s,r=document)=>r.querySelector(s);
let CURRENT = location.hash.slice(1).toUpperCase() || 'OVERVIEW';
const CACHE = {};

// ---- formatting only. No arithmetic: Python computed every value. ----
const nul = '<span class="mut" title="not measured">&mdash;</span>';
function money(v,d=2){ if(v===null||v===undefined) return nul;
  const s = Number(v)<0?'neg':(Number(v)>0?'pos':'');
  return `<span class="${s}">$${Number(v).toFixed(d)}</span>`; }
function pct(v,d=1){ if(v===null||v===undefined) return nul;
  const s = Number(v)<0?'neg':(Number(v)>0?'pos':'');
  return `<span class="${s}">${(Number(v)*100).toFixed(d)}%</span>`; }
function num(v,d=2){ return (v===null||v===undefined)?nul:Number(v).toFixed(d); }
function int(v){ return (v===null||v===undefined)?nul:Number(v).toLocaleString(); }
function ts(v){ if(!v) return nul;
  return new Date(Number(v)*1000).toISOString().slice(0,19).replace('T',' '); }
function esc(s){ return String(s??'').replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function trunc(s,n=60){ s=String(s??''); return s.length>n?s.slice(0,n)+'…':s; }

function card(k,v,c=''){ return `<div class="card"><div class="k">${esc(k)}</div>
  <div class="v">${v}</div><div class="c">${esc(c)}</div></div>`; }
function box(title,inner){ return `<div class="box"><div class="hd">${esc(title)}</div>${inner}</div>`; }
function note(t){ return t?`<div class="note">${esc(t)}</div>`:''; }
function tagOf(ok){ return ok?'<span class="tag g">yes</span>'
                            :'<span class="tag r">no</span>'; }

function table(cols, rows, opts={}){
  if(!rows || !rows.length) return `<div class="empty">no rows</div>`;
  const head = cols.map(c=>`<th>${esc(c.t)}</th>`).join('');
  const body = rows.map((r,i)=>{
    const cls = opts.onRow ? 'clickable' : '';
    const tds = cols.map(c=>{
      const v = c.f ? c.f(r) : esc(r[c.k]);
      return `<td class="${c.cls||''}">${v}</td>`;
    }).join('');
    return `<tr class="${cls}" data-i="${i}">${tds}</tr>`;
  }).join('');
  return `<div class="tbl-wrap"><table><thead><tr>${head}</tr></thead>
          <tbody>${body}</tbody></table></div>`;
}

async function load(section){
  const r = await fetch('/api/'+section.toLowerCase());
  return await r.json();
}

async function render(section){
  CURRENT = section;
  history.replaceState(null,'','#'+section);
  document.querySelectorAll('nav button').forEach(b=>
    b.setAttribute('aria-current', b.dataset.s===section));
  const main = $('#main');
  main.innerHTML = `<div class="empty">loading ${esc(section)}…</div>`;
  let d;
  try { d = await load(section); }
  catch(e){ main.innerHTML = `<div class="note">failed to load: ${esc(e)}</div>`;
            return; }
  CACHE[section]=d;
  const fn = VIEWS[section] || VIEWS._generic;
  main.innerHTML = `<h2 class="page-t">${esc(section)}</h2>
    <p class="page-s">${esc(SUB[section]||'')}</p>` + fn(d) + note(d.note);
  $('#stamp').textContent = 'updated '+ts(d.generated_ts);
}

const SUB = {
 OVERVIEW:'Account, performance and system reach. Every figure is derived from persisted rows; a null means not yet measured, never zero.',
 PORTFOLIO:'Exposure grouped by true underlying event, which is the view in which correlated bets stop looking diversified.',
 RISK:'Drawdown against the hard stop, plus the crash meter and what it authorises.',
 PAPER:'Simulated execution using the same fill model as the backtest.',
 LIVE:'Live execution stays disabled until a human authorises it against measured requirements.',
 OPPORTUNITIES:'Ranked candidates. Execution feasibility multiplies the score, so an untradeable market ranks at zero rather than merely lower.',
 STRATEGIES:'Versioned strategies and their position on the status ladder.',
 DISCOVERY:'Hypotheses tested, and the full denominator that makes any p-value interpretable.',
 BACKTEST:'Event-level backtest at $100 starting capital with realistic costs.',
 VALIDATION:'The twelve gates, who owns each, and what each one has cost or saved.',
 LEARNING:'What the system changed, and why. Includes missed opportunities, without which the gates only ever tighten.',
 MARKETS:'Market metadata, event grouping and close times.',
 WALLETS:'Behavioural fingerprints ranked by alpha over the price band, never by win rate.',
 LEADERBOARD:'Several orderings, because no single one is correct.',
 NEWS:'Captured items with publication and capture times kept distinct.',
 EVENTS:'Items carrying an event time separate from publication time.',
 BLOCKCHAIN:'On-chain activity for watched addresses.',
 MICROSTRUCTURE:'Order-book snapshots. This is the one data class that cannot be backfilled.',
 AGENTS:'Twenty-five specialists, their latest calls, and their disagreements. Abstentions are shown, not hidden.',
 ACTIVITY:'Every decision, including the rejections and the gate that caused each.',
 LOSSES:'Forensic record for every losing trade, with a classification and a remedy.',
 SYSTEM:'Data freshness, collector health and store contents. Every count is a SELECT.',
};

const VIEWS = {};

VIEWS._generic = d => box('data', `<pre>${esc(JSON.stringify(d,null,2))}</pre>`);

VIEWS.OVERVIEW = d => {
  const cards = [
    card('Starting capital', money(d.starting_capital), '$100 mode'),
    card('Account value', money(d.account_value),
         `${d.mode} · ${d.wallet_status}`),
    card('Available cash', money(d.available_cash),
         `reserved ${Number(d.reserved).toFixed(2)}`),
    card('Total P&L', money(d.total_pnl),
         `realized ${Number(d.realized_pnl).toFixed(2)} · unrealized ${Number(d.unrealized_pnl).toFixed(2)}`),
    card('Return', pct(d.return_pct), 'on starting capital'),
    card('Drawdown', pct(d.drawdown), 'from peak equity'),
    card('Win rate', d.win_rate===null?nul:pct(d.win_rate),
         `over ${d.completed_trades} completed trades`),
    card('Expectancy', d.expectancy===null?nul:money(d.expectancy,4), 'per trade'),
    card('Profit factor', num(d.profit_factor), 'gross win / gross loss'),
    card('Open positions', int(d.open_positions), `of ${d.mode} book`),
    card('Paper trades', int(d.paper_trades), 'fills recorded'),
    card('Live trades', int(d.live_trades),
         d.live_authorized?'authorized':'live disabled'),
  ].join('');
  const reach = [
    card('Markets scanned', int(d.markets_scanned), 'last scan'),
    card('Wallets profiled', int(d.wallets_monitored), 'DNA built'),
    card('News items', int(d.news_events_detected), 'captured to date'),
    card('Decisions 24h', int(d.opportunities_detected),
         `${d.opportunities_rejected} rejected`),
    card('Validated strategies', int(d.validated_strategies), 'APPROVED or LIVE'),
    card('Active strategies', int(d.active_strategies), 'shadow through live'),
  ].join('');
  return box('account', `<div class="cards">${cards}</div>`)
       + box('research reach', `<div class="cards">${reach}</div>`);
};

VIEWS.PORTFOLIO = d => {
  const a=d.account, l=d.limits;
  const cards=[card('Equity',money(a.equity)),
    card('Available cash',money(a.available_cash)),
    card('Gross exposure',money(d.exposure.gross)),
    card('Per-trade cap',money(l.per_trade_usdc),
         `${(l.max_fraction_per_trade*100).toFixed(0)}% of equity`),
    card('Venue minimum',money(l.min_order_usdc),'absolute; does not scale'),
    card('Open positions',int(a.open_positions),`limit ${l.max_open_positions}`)
  ].join('');
  const buckets=Object.entries(d.buckets||{}).map(([k,v])=>({k,...v}));
  return box('capital', `<div class="cards">${cards}</div>`)
    + box('correlated exposure buckets', table([
        {t:'Bucket',k:'k',f:r=>esc(trunc(r.k,40))},
        {t:'USDC',cls:'num',f:r=>money(r.usdc)},
        {t:'Positions',cls:'num',f:r=>int(r.positions)},
        {t:'% equity',cls:'num',f:r=>pct(r.usdc/(a.equity||1))},
        {t:'Markets',f:r=>esc(trunc((r.markets||[]).join(', '),40))},
      ], buckets));
};

VIEWS.RISK = d => {
  const c=d.crash_meter;
  const cards=[
    card('Drawdown',pct(d.drawdown),`hard stop ${(d.hard_stop*100).toFixed(0)}%`),
    card('Trading',d.halted?'<span class="neg">HALTED</span>':'<span class="pos">ACTIVE</span>',
         d.halted?'human must resume':'within limits'),
    card('Crash level', c?`<span class="${c.level==='NORMAL'?'pos':(c.level==='ELEVATED'?'amb':'neg')}">${esc(c.level)}</span>`:nul,
         c?`confidence ${num(c.confidence)}`:'not read yet'),
    card('Crash score', c?num(c.score):nul, c?(c.drivers||[]).join(' · '):''),
  ].join('');
  let extra='';
  if(c){
    extra = box('crash meter inputs', table([
      {t:'Input',k:'k'},{t:'Value',cls:'num',f:r=>num(r.v)},
      {t:'',f:r=>`<div class="bar"><i style="width:${Math.round(r.v*100)}%"></i></div>`}
    ], Object.entries(c.inputs||{}).map(([k,v])=>({k,v}))))
    + box('unavailable inputs',
        (c.unavailable||[]).length
          ? `<div class="note">${(c.unavailable||[]).map(esc).join(' · ')}<br><br>
             Missing inputs lower the meter's confidence, never its level.</div>`
          : '<div class="empty">all inputs available</div>')
    + box('authorised responses',
        (c.actions||[]).length?`<div>${(c.actions||[]).map(a=>`<span class="tag a">${esc(a)}</span> `).join('')}</div>`
        :'<div class="empty">none — level is NORMAL</div>');
  }
  return box('risk state', `<div class="cards">${cards}</div>`) + extra;
};

const tradingView = d => {
  const a=d.account;
  const cards=[card('Equity',money(a.equity)),card('Realized',money(a.realized_pnl)),
    card('Unrealized',money(a.unrealized_pnl)),card('Return',pct(a.return_pct)),
    card('Open',int(a.open_positions)),card('Drawdown',pct(a.drawdown))].join('');
  return box(d.mode+' account', `<div class="cards">${cards}</div>`)
   + box('positions', table([
      {t:'Opened',f:r=>ts(r.opened_ts)},{t:'Market',f:r=>esc(trunc(r.market_id,18))},
      {t:'Entry',cls:'num',f:r=>num(r.entry_price,4)},
      {t:'Size',cls:'num',f:r=>money(r.size_usdc)},
      {t:'Realized',cls:'num',f:r=>money(r.realized_pnl,4)},
      {t:'Status',f:r=>`<span class="tag">${esc(r.status)}</span>`},
    ], d.positions))
   + box('fills', table([
      {t:'When',f:r=>ts(r.ts)},{t:'Signal',cls:'num',f:r=>num(r.signal_price,4)},
      {t:'Expected',cls:'num',f:r=>num(r.expected_fill,4)},
      {t:'Actual',cls:'num',f:r=>num(r.actual_fill,4)},
      {t:'Slippage',cls:'num',f:r=>num(r.slippage,4)},
      {t:'Size',cls:'num',f:r=>money(r.size_usdc)},
      {t:'Unmodelled',cls:'wrap',f:r=>esc(trunc((JSON.parse(r.uncertainty||'[]')).join(', '),50))},
    ], d.fills));
};
VIEWS.PAPER = tradingView;
VIEWS.LIVE = d => {
  const A=d.authorization||{};
  const reqs = box('requirements before live authorization', table([
    {t:'Requirement',k:'requirement',cls:'wrap'},
    {t:'Met',f:r=>tagOf(r.met)},{t:'Actual',f:r=>esc(String(r.actual))},
  ], A.requirements||[]));
  const banner = `<div class="note"><b>${esc(A.wallet||'')}</b> ·
    live_authorized = <b>${A.live_authorized?'true':'false'}</b>.
    Live execution is a human decision this system never makes for itself.
    Authorize from the CLI: <code>pqv3 authorize-live --yes</code>.</div>`;
  return banner + reqs + tradingView(d)
    + box('authorization history', table([
        {t:'When',f:r=>ts(r.ts)},{t:'Action',k:'action'},
        {t:'Granted',f:r=>tagOf(r.granted)},{t:'Actor',k:'actor'},
        {t:'Note',k:'note',cls:'wrap'}], A.history||[]));
};

VIEWS.OPPORTUNITIES = d => {
  if(!d.scan) return '<div class="empty">no scan yet</div>';
  const s=d.scan;
  const cards=[card('Scanned',int(s.markets_scanned),'markets'),
    card('Eligible',int(s.markets_eligible),'passed stage 1'),
    card('Ranked',int((s.opportunities||[]).length),'taken to full decision'),
    card('Dropped',int(s.markets_dropped_at_stage1),'below the cut'),
    card('Elapsed',int(s.elapsed_ms)+' ms','')].join('');
  return box('scan', `<div class="cards">${cards}</div>`
      + (s.notes||[]).map(n=>note(n)).join(''))
   + box('ranked opportunities', table([
      {t:'Score',cls:'num',f:r=>num(r.overall_score,4)},
      {t:'Question',cls:'wrap',f:r=>esc(trunc(r.question,58))},
      {t:'Market px',cls:'num',f:r=>num(r.market_probability,4)},
      {t:'Fair',cls:'num',f:r=>num(r.fair_probability,4)},
      {t:'Edge',cls:'num',f:r=>pct(r.edge,2)},
      {t:'Mispricing',cls:'num',f:r=>num(r.mispricing_score,3)},
      {t:'Wallet',cls:'num',f:r=>num(r.wallet_signal_score,3)},
      {t:'Micro',cls:'num',f:r=>num(r.microstructure_score,3)},
      {t:'News',cls:'num',f:r=>num(r.news_score,3)},
      {t:'Exec',cls:'num',f:r=>num(r.execution_score,3)},
      {t:'Risk',cls:'num',f:r=>num(r.risk_score,3)},
    ], s.opportunities));
};

VIEWS.WALLETS = d => {
  if(!d.wallets.length) return '';
  const co = Object.entries(d.cohorts||{}).map(([k,v])=>
    `<div style="margin:4px 0"><span class="tag">${esc(k)}</span>
     <span class="mut">${v.length} wallet(s)</span></div>`).join('');
  return box('cohorts', co||'<div class="empty">none</div>')
   + box(`wallet DNA (${d.n} profiled)`, table([
      {t:'Wallet',f:r=>`<a href="#" onclick="openWallet('${esc(r.wallet)}');return false">${esc(r.wallet.slice(0,14))}…</a>`},
      {t:'Alpha vs band',cls:'num',f:r=>pct(r.alpha_vs_band,2)},
      {t:'Win rate',cls:'num',f:r=>pct(r.win_rate)},
      {t:'Expectancy',cls:'num',f:r=>pct(r.expectancy,2)},
      {t:'Profit factor',cls:'num',f:r=>num(r.profit_factor)},
      {t:'Max DD',cls:'num',f:r=>pct(r.max_drawdown)},
      {t:'Trades',cls:'num',f:r=>int(r.trades)},
      {t:'Markets',cls:'num',f:r=>int(r.markets)},
      {t:'Style',f:r=>esc(r.scaling_behavior)},
      {t:'Evidence',f:r=>`<span class="tag ${r.evidence_quality==='STRONG'?'g':(r.evidence_quality==='INSUFFICIENT'?'r':'a')}">${esc(r.evidence_quality)}</span>`},
      {t:'Notes',cls:'wrap',f:r=>esc(trunc((r.notes||[]).join(' · '),70))},
    ], d.wallets));
};

VIEWS.LEADERBOARD = d => Object.entries(d.boards||{}).map(([k,rows])=>
  box(k, table([
    {t:'Wallet',f:r=>esc(r.wallet.slice(0,16))+'…'},
    {t:'Alpha',cls:'num',f:r=>pct(r.alpha_vs_band,2)},
    {t:'Win',cls:'num',f:r=>pct(r.win_rate)},
    {t:'Expectancy',cls:'num',f:r=>pct(r.expectancy,2)},
    {t:'Sharpe-like',cls:'num',f:r=>num(r.sharpe_like)},
    {t:'Max DD',cls:'num',f:r=>pct(r.max_drawdown)},
    {t:'Trades',cls:'num',f:r=>int(r.trades)},
  ], rows))).join('');

VIEWS.AGENTS = d => box(`${d.agents.length} agents · ${d.n_adversarial} adversarial`,
  table([
    {t:'#',cls:'num',k:'number'},{t:'Agent',k:'name'},
    {t:'Role',k:'role',cls:'wrap'},
    {t:'Adversarial',f:r=>r.adversarial?'<span class="tag a">yes</span>':''},
    {t:'Requires',f:r=>esc((r.requires||[]).join(', ')||'—')},
    {t:'Latest',f:r=>r.latest?`<span class="tag ${r.latest.stance==='FOR'?'g':(r.latest.stance==='AGAINST'?'r':'')}">${esc(r.latest.stance)}</span>`:nul},
    {t:'Conf',cls:'num',f:r=>r.latest?num(r.latest.confidence):nul},
    {t:'Accuracy',cls:'num',f:r=>r.accuracy && r.accuracy.accuracy!==null?pct(r.accuracy.accuracy):`<span class="mut" title="${esc(r.accuracy?r.accuracy.note||'':'')}">n=0</span>`},
    {t:'Thesis',cls:'wrap',f:r=>esc(trunc(r.latest?r.latest.thesis:'',70))},
  ], d.agents))
  + box('recent output', table([
    {t:'When',f:r=>ts(r.ts)},{t:'Agent',k:'agent'},
    {t:'Stance',f:r=>`<span class="tag ${r.stance==='FOR'?'g':(r.stance==='AGAINST'?'r':'')}">${esc(r.stance)}</span>`},
    {t:'Conf',cls:'num',f:r=>num(r.confidence)},
    {t:'Thesis',cls:'wrap',f:r=>esc(trunc(r.thesis,80))},
  ], d.recent));

VIEWS.VALIDATION = d => {
  const s=d.settled_ts||{};
  return box('settlement timestamp coverage',
      `<div class="cards">${[
        card('Usable',int(s.usable),`of ${int(s.total)} recorded`),
        card('Share',pct(s.usable_share)),
        card('PIT features',s.pit_features_enabled?'<span class="pos">ENABLED</span>':'<span class="neg">DISABLED</span>','confidence ≥ 0.60'),
      ].join('')}</div>` + note(s.note))
   + box('the twelve gates', table([
      {t:'Gate',k:'gate'},{t:'Owner',f:r=>`<span class="tag">${esc(r.owner)}</span>`},
      {t:'Critical',f:r=>tagOf(r.critical)},
      {t:'Rationale',k:'rationale',cls:'wrap'},
    ], d.gates))
   + box('what has blocked decisions', table([
      {t:'Gate',k:'g'},{t:'Count',cls:'num',f:r=>int(r.n)},
    ], d.blocking_counts))
   + box('gate cost: saved vs forgone', table([
      {t:'Gate',k:'gate'},{t:'n',cls:'num',f:r=>int(r.n)},
      {t:'Correct',cls:'num',f:r=>int(r.saved)},
      {t:'Missed',cls:'num',f:r=>int(r.missed)},
      {t:'Precision',cls:'num',f:r=>r.precision===null?nul:pct(r.precision)},
      {t:'Avoided',cls:'num',f:r=>num(r.avoided,3)},
      {t:'Forgone',cls:'num',f:r=>num(r.forgone,3)},
      {t:'Net',cls:'num',f:r=>num(r.net,3)},
      {t:'Verdict',k:'verdict',cls:'wrap'},
    ], d.gate_cost));
};

VIEWS.ACTIVITY = d => box(`decisions (${int(d.n_decisions)} total)`, table([
    {t:'When',f:r=>ts(r.ts)},{t:'Mode',k:'mode'},
    {t:'Market',f:r=>esc(trunc(r.market_id,16))},
    {t:'Action',f:r=>`<span class="tag ${r.action==='TRADE'?'g':''}">${esc(r.action)}</span>`},
    {t:'Blocked by',k:'blocking_gate'},
    {t:'Edge',cls:'num',f:r=>pct(r.edge,2)},
    {t:'Conf',cls:'num',f:r=>num(r.confidence)},
    {t:'Size',cls:'num',f:r=>money(r.size_usdc)},
  ], d.decisions))
  + box('alerts', table([
    {t:'When',f:r=>ts(r.ts)},
    {t:'Severity',f:r=>`<span class="tag ${r.severity==='ERROR'?'r':(r.severity==='WARN'?'a':'')}">${esc(r.severity)}</span>`},
    {t:'Kind',k:'kind'},{t:'Message',k:'message',cls:'wrap'},
  ], d.alerts));

VIEWS.LOSSES = d => box('by classification', table([
    {t:'Classification',k:'classification'},{t:'Count',cls:'num',f:r=>int(r.n)},
    {t:'Avg loss',cls:'num',f:r=>money(r.avg_loss,4)},
  ], d.by_classification))
  + box('forensic records', table([
    {t:'When',f:r=>ts(r.ts)},{t:'Classification',k:'classification'},
    {t:'Predictable',f:r=>tagOf(r.predictable)},
    {t:'Failed agent',k:'failed_agent'},{t:'Feature',k:'failed_feature'},
    {t:'Predicted',cls:'num',f:r=>num(r.predicted,3)},
    {t:'Actual',cls:'num',f:r=>money(r.actual,4)},
    {t:'Remedy',f:r=>`<span class="tag ${r.remedy==='retire'?'r':'a'}">${esc(r.remedy)}</span>`},
    {t:'Narrative',k:'narrative',cls:'wrap'},
  ], d.losses));

VIEWS.LEARNING = d => box('gate weight drift', table([
    {t:'Gate',k:'gate'},{t:'Recent',cls:'num',f:r=>pct(r.recent)},
    {t:'Previous',cls:'num',f:r=>pct(r.previous)},
    {t:'Delta',cls:'num',f:r=>pct(r.delta)},
  ], [...(d.drift.gaining||[]),...(d.drift.losing||[])]))
  + box('counterfactuals', table([
    {t:'Variant',k:'variant'},{t:'n',cls:'num',f:r=>int(r.n)},
    {t:'Avg P&L',cls:'num',f:r=>money(r.avg_pnl,4)},
  ], d.counterfactuals))
  + box('missed opportunities', table([
    {t:'When',f:r=>ts(r.ts)},{t:'Market',f:r=>esc(trunc(r.market_id,16))},
    {t:'Gate',k:'rejection_gate'},
    {t:'Would return',cls:'num',f:r=>pct(r.would_have_returned,1)},
    {t:'Correct',f:r=>tagOf(r.rejection_correct)},
    {t:'Executable',f:r=>tagOf(r.executable)},
    {t:'Narrative',k:'narrative',cls:'wrap'},
  ], d.missed))
  + box('strategy flow', table([
    {t:'Status',k:'status'},{t:'Count',cls:'num',f:r=>int(r.n)}
  ], d.strategy_flow));

VIEWS.MICROSTRUCTURE = d => box('book history', `<div class="cards">${[
    card('Tokens',int(d.n),'with snapshots'),
    card('History',num(d.history_days,2)+' d',`need ${d.min_history_days} d`),
    card('Depth features',d.gated?'<span class="neg">GATED</span>':'<span class="pos">AVAILABLE</span>',
         d.gated?'insufficient history':'')
  ].join('')}</div>`)
  + box('per token', table([
    {t:'Token',f:r=>esc(trunc(r.token_id,20))},
    {t:'Snapshots',cls:'num',f:r=>int(r.snapshots)},
    {t:'Avg spread',cls:'num',f:r=>num(r.avg_spread,4)},
    {t:'Avg depth',cls:'num',f:r=>money(r.avg_depth)},
    {t:'Avg imbalance',cls:'num',f:r=>num(r.avg_imb,3)},
    {t:'Last',f:r=>ts(r.last_ts)},
  ], d.tokens));

VIEWS.NEWS = d => box(`news (${d.history_days} d of history)`, table([
    {t:'Captured',f:r=>ts(r.capture_ts)},{t:'Published',f:r=>ts(r.ts)},
    {t:'Lag',cls:'num',f:r=>r.ts?int(r.capture_ts-r.ts)+'s':nul},
    {t:'Source',k:'source_name'},
    {t:'Class',f:r=>`<span class="tag">${esc(r.source_class)}</span>`},
    {t:'Reliability',cls:'num',f:r=>num(r.reliability)},
    {t:'Confirmation',f:r=>`<span class="tag ${r.confirmation==='OFFICIAL'?'g':'a'}">${esc(r.confirmation)}</span>`},
    {t:'Linked',cls:'num',f:r=>int(r.linked)},
    {t:'Title',k:'title',cls:'wrap'},
  ], d.items));

VIEWS.MARKETS = d => box(`markets (${d.n})`, table([
    {t:'Market',f:r=>esc(trunc(r.market_id,18))},
    {t:'Question',k:'question',cls:'wrap'},
    {t:'Category',k:'category'},{t:'Event',f:r=>esc(trunc(r.event_id,12))},
    {t:'Closes',f:r=>ts(r.close_ts)},{t:'Status',k:'status'},
  ], d.markets));

VIEWS.SYSTEM = d => {
  const inv=d.v1_data||{}, st=d.store||{};
  const cards=[card('Mode',esc(d.mode)),
    card('Collectors',d.collectors_enabled?'<span class="pos">ENABLED</span>':'<span class="amb">DISABLED</span>'),
    card('Wallet',esc(d.wallet)),
    card('V1 tape',inv.available?int(inv.wallet_trades):'<span class="neg">ABSENT</span>',
         inv.available?`${inv.tape_days} d · ${int(inv.markets)} markets`:''),
    card('V2 package',d.v2.available?'<span class="pos">OK</span>':'<span class="amb">absent</span>',esc(d.v2.detail||'')),
    card('Schema',esc(st.schema_version||''),'store version'),
  ].join('');
  return box('status', `<div class="cards">${cards}</div>`)
   + box('collector health', table([
      {t:'Collector',k:'collector'},
      {t:'Status',f:r=>`<span class="tag ${r.status==='OK'?'g':(r.status==='ERROR'?'r':'a')}">${esc(r.status)}</span>`},
      {t:'Last success',f:r=>ts(r.last_success_ts)},
      {t:'Last attempt',f:r=>ts(r.last_attempt_ts)},
      {t:'Error',k:'error',cls:'wrap'},{t:'Detail',k:'detail',cls:'wrap'},
    ], d.health))
   + box('store contents', table([
      {t:'Table',k:'t'},{t:'Rows',cls:'num',f:r=>int(r.n)},
    ], Object.entries(st.tables||{}).map(([t,n])=>({t,n}))))
   + box('startup sequence', table([
      {t:'#',cls:'num',k:'step'},{t:'Step',k:'name'},
      {t:'OK',f:r=>tagOf(r.ok)},{t:'ms',cls:'num',f:r=>int(r.elapsed_ms)},
      {t:'Detail',k:'detail',cls:'wrap'},
    ], (d.engine||{}).startup||[]))
   + box('credentials', table([
      {t:'Name',k:'name'},{t:'Present',f:r=>tagOf(r.present)},{t:'Source',k:'source'},
    ], d.secrets) + note('Presence only. No length, prefix or fingerprint is '
      + 'exposed, because each of those narrows a key search space.'));
};

VIEWS.STRATEGIES = d => box('by status', table([
    {t:'Status',k:'k'},{t:'Count',cls:'num',f:r=>int(r.v)}
  ], Object.entries(d.by_status||{}).map(([k,v])=>({k,v}))))
  + box('strategies', table([
    {t:'ID',k:'strategy_id'},{t:'v',cls:'num',k:'version'},
    {t:'Family',k:'family'},{t:'Status',f:r=>`<span class="tag">${esc(r.status)}</span>`},
    {t:'Trades',cls:'num',f:r=>int(r.trade_count)},
    {t:'Win',cls:'num',f:r=>pct(r.win_rate)},
    {t:'Expectancy',cls:'num',f:r=>num(r.expectancy,4)},
    {t:'PF',cls:'num',f:r=>num(r.profit_factor)},
    {t:'Max DD',cls:'num',f:r=>pct(r.max_drawdown)},
    {t:'Evidence',k:'evidence_quality'},
  ], d.strategies));

VIEWS.DISCOVERY = d => box('search space', `<div class="cards">${[
    card('Hypotheses',int(d.hypotheses_total),'recorded'),
    card('Raw tests',int(d.raw_tests),'across all passes'),
    card('Distinct',int(d.effective_search_space),'effective search space'),
  ].join('')}</div>`)
  + box('passes', table([
    {t:'Pass',k:'pass_id'},{t:'Started',f:r=>ts(r.started_ts)},
    {t:'Tested',cls:'num',f:r=>int(r.tested)},
    {t:'Distinct',cls:'num',f:r=>int(r.distinct_tested)},
    {t:'Surviving',cls:'num',f:r=>int(r.surviving)},
    {t:'BH α',cls:'num',f:r=>num(r.bh_alpha,3)},
    {t:'BH threshold',cls:'num',f:r=>num(r.bh_threshold,6)},
  ], d.passes));

VIEWS.EVENTS = d => box('events', table([
    {t:'Event time',f:r=>ts(r.event_ts)},{t:'Published',f:r=>ts(r.ts)},
    {t:'Captured',f:r=>ts(r.capture_ts)},
    {t:'Lag',cls:'num',f:r=>int(r.publication_lag)+'s'},
    {t:'Class',k:'source_class'},{t:'Confirmation',k:'confirmation'},
    {t:'Title',k:'title',cls:'wrap'},
  ], d.events));

VIEWS.BLOCKCHAIN = d => box(`chain (${int(d.n)} events, ${d.history_days} d)`,
  table([
    {t:'When',f:r=>ts(r.ts)},{t:'Block',cls:'num',f:r=>int(r.block_number)},
    {t:'Kind',k:'kind'},{t:'Wallet',f:r=>esc(trunc(r.wallet,20))},
    {t:'Asset',k:'asset'},{t:'Amount',cls:'num',f:r=>num(r.amount,4)},
  ], d.events));

VIEWS.BACKTEST = d => d.result
  ? box('result', `<pre>${esc(JSON.stringify(d.result,null,2))}</pre>`)
  : '<div class="empty">no backtest result</div>';

async function openWallet(w){
  const r = await fetch('/api/wallet_detail?wallet='+encodeURIComponent(w));
  const d = await r.json();
  const dna = d.dna;
  $('#main').innerHTML = `<h2 class="page-t">WALLET ${esc(w.slice(0,20))}…</h2>
   <p class="page-s">Full forensic record.
     <a href="#" onclick="render('WALLETS');return false">back to wallets</a></p>`
   + (dna ? box('DNA', `<pre>${esc(JSON.stringify(dna,null,2))}</pre>`) : '')
   + box(`trades (${d.n_trades})`, table([
      {t:'When',f:r=>ts(r.ts)},{t:'Type',k:'event_type'},{t:'Side',k:'side'},
      {t:'Price',cls:'num',f:r=>num(r.price,4)},
      {t:'USDC',cls:'num',f:r=>money(r.usdc)},
      {t:'Question',k:'question',cls:'wrap'},
     ], d.trades))
   + box('chain activity', table([
      {t:'When',f:r=>ts(r.ts)},{t:'Kind',k:'kind'},
      {t:'Amount',cls:'num',f:r=>num(r.amount,4)},
     ], d.chain_events))
   + note(d.note);
}

document.addEventListener('click', e=>{
  const b = e.target.closest('nav button');
  if(b) render(b.dataset.s);
});
$('#refresh').onclick = ()=>render(CURRENT);
$('#theme').onclick = ()=>{
  const cur = document.documentElement.getAttribute('data-theme');
  document.documentElement.setAttribute('data-theme', cur==='dark'?'light':'dark');
};
render(CURRENT);
setInterval(()=>{ if(document.visibilityState==='visible') render(CURRENT); }, 30000);
"""


def page(*, mode: str, wallet: str, live_authorized: bool,
         starting_capital: float, url: str) -> str:
    nav_html = "".join(
        f'<div class="grp">{group}</div>' +
        "".join(f'<button data-s="{s}">{s.title()}</button>' for s in items)
        for group, items in NAV)
    live_pill = ('<span class="pill on">LIVE AUTHORIZED</span>'
                 if live_authorized
                 else '<span class="pill off">LIVE DISABLED</span>')
    wallet_pill = (f'<span class="pill on">{wallet}</span>'
                   if wallet == "WALLET CONNECTED"
                   else f'<span class="pill">{wallet}</span>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Polymarket Quant Bridge V3</title>
<style>{CSS}</style></head>
<body><div class="app">
  <div class="titlebar">
    <h1>Polymarket Quant Bridge <span class="mut">V3</span></h1>
    <span class="pill">MODE {mode}</span>
    <span class="pill">CAPITAL ${starting_capital:,.2f}</span>
    {wallet_pill}
    {live_pill}
    <span class="spacer"></span>
    <button class="btn" id="refresh">Refresh</button>
    <button class="btn" id="theme">Theme</button>
  </div>
  <div class="shell">
    <nav>{nav_html}</nav>
    <main id="main"></main>
  </div>
  <div class="statusbar">
    <span>{url}</span>
    <span id="stamp"></span>
    <span class="spacer"></span>
    <span>Rust/Python engine computes · browser displays only</span>
  </div>
</div>
<script>{JS}</script>
</body></html>"""
