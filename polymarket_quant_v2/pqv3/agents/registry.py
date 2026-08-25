"""The twenty-five agents.

Each one is a small, deterministic function over the evidence state. They are
deliberately NOT language-model calls: an LLM asked "is this a good trade"
produces a fluent number with no error bars, and the brief is explicit that an
LLM must never do arithmetic Rust or Python can do deterministically. The LLM
layer (`llm.py`) sits beside these and is used for *narrative* — summarising a
news item, proposing a hypothesis in words — never for a probability.

They also do not all see the same thing. Agent 1 reads microstructure, Agent 4
reads news, Agent 12 is paid to disagree. That is what makes their disagreement
informative; twenty-five agents reading the same three numbers would produce
twenty-five copies of one opinion and a consensus score of 1.0 that means
nothing.

Where an agent has nothing to say — because its layer is empty on a fresh
install — it abstains and the debate records that. On a system with no news
feed and no order book configured, roughly ten of these will abstain, the
consensus will be computed over the rest, and the dashboard will say so.
"""

from __future__ import annotations

import math
import statistics

from ..core.canon import EvidenceState
from .base import AgentSpec, Stance, Verdict, clamp, linear_confidence

# `ctx` carries the non-evidence inputs a few agents legitimately need:
#   market_probability, fair_probability, strategy (dict), sizing (SizingResult),
#   account (Account), wallet_dna (dict), history (list of prior verdicts).
# It never carries an outcome.


# --------------------------------------------------------------------------
# 1 — MARKET MICROSTRUCTURE
# --------------------------------------------------------------------------
def a01_microstructure(ev: EvidenceState, ctx: dict) -> Verdict:
    px, liq = ev.price, ev.liquidity
    vel = float(px.get("velocity_1h") or 0.0)
    accel = float(px.get("acceleration") or 0.0)
    prints = float(liq.get("prints_per_hour") or 0.0)
    gap = float(px.get("gap") or 0.0)
    ev_list = [f"velocity_1h={vel:+.4f}", f"acceleration={accel:+.4f}",
               f"prints/h={prints:.1f}", f"last_gap={gap:+.4f}"]

    if prints < 2:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"only {prints:.1f} prints/hour; "
                                      f"microstructure is unreadable",
                       evidence=ev_list)
    # Absorption: heavy printing with little price movement means someone is
    # taking the other side in size. Directionally informative.
    absorbed = prints >= 10 and abs(vel) < 0.01
    exhaustion = abs(vel) > 0.05 and accel * vel < 0      # move decelerating

    if exhaustion:
        return Verdict("", Stance.AGAINST,
                       confidence=linear_confidence(abs(accel), 0.01, 0.06),
                       thesis="momentum is decelerating against the recent move; "
                              "entering here buys the end of a leg",
                       evidence=ev_list,
                       objections=["deceleration can precede continuation"])
    if absorbed:
        return Verdict("", Stance.FOR,
                       confidence=linear_confidence(prints, 10, 60) * 0.6,
                       thesis="high print rate with a flat price implies "
                              "absorption; a resting bid is defending this level",
                       evidence=ev_list)
    if abs(vel) > 0.02 and accel * vel > 0:
        return Verdict("", Stance.FOR if vel > 0 else Stance.AGAINST,
                       confidence=linear_confidence(abs(vel), 0.02, 0.10),
                       thesis=f"price is moving {'up' if vel > 0 else 'down'} "
                              f"and accelerating",
                       evidence=ev_list)
    return Verdict("", Stance.ABSTAIN,
                   abstain_reason="no microstructural pattern above the noise floor",
                   evidence=ev_list)


# --------------------------------------------------------------------------
# 2 — WALLET FORENSICS
# --------------------------------------------------------------------------
def a02_wallet_forensics(ev: EvidenceState, ctx: dict) -> Verdict:
    dna = ctx.get("wallet_dna") or {}
    w = ev.wallets
    top = w.get("top") or []
    if not top:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no wallet activity in the window")
    known = [t for t in top if t["wallet"] in dna]
    if not known:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"{len(top)} wallets active, none with a "
                                      f"built DNA profile; run `pqv3 dna`")
    # Weight each wallet's stance by its ALPHA, not its win rate. A wallet with
    # a 70% win rate that only ever buys 0.75 favourites has no alpha at all on
    # this dataset — the price band alone delivers that.
    num = den = 0.0
    lines = []
    for t in known:
        d = dna[t["wallet"]]
        alpha = float(d.get("alpha_vs_band") or 0.0)
        wgt = abs(alpha) * math.log1p(float(t.get("notional") or 0.0))
        num += alpha * wgt
        den += wgt
        lines.append(f"{t['wallet'][:10]} alpha={alpha:+.3f} "
                     f"notional=${t.get('notional', 0):.0f}")
    if den <= 0:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="active wallets have no measured alpha",
                       evidence=lines)
    score = num / den
    return Verdict("", Stance.FOR if score > 0 else Stance.AGAINST,
                   confidence=linear_confidence(abs(score), 0.01, 0.12),
                   thesis=f"alpha-weighted wallet flow is {score:+.3f} "
                          f"across {len(known)} profiled wallets",
                   evidence=lines[:8],
                   objections=[] if len(known) >= 3 else
                   ["fewer than 3 profiled wallets; this is one opinion, "
                    "not a consensus"])


# --------------------------------------------------------------------------
# 3 — BLOCKCHAIN FORENSICS
# --------------------------------------------------------------------------
def a03_blockchain(ev: EvidenceState, ctx: dict) -> Verdict:
    by_kind = ev.blockchain.get("by_kind") or {}
    if not by_kind:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no chain events in the window")
    inflow = float((by_kind.get("TRANSFER") or {}).get("amount") or 0.0)
    redeems = int((by_kind.get("REDEEM") or {}).get("n") or 0)
    lines = [f"{k}: n={v.get('n')} amount={v.get('amount')}"
             for k, v in by_kind.items()]
    if redeems > 0:
        return Verdict("", Stance.AGAINST, confidence=0.35,
                       thesis=f"{redeems} redemptions observed; participants are "
                              f"realising, not accumulating",
                       evidence=lines)
    if inflow > 0:
        return Verdict("", Stance.FOR,
                       confidence=linear_confidence(math.log1p(inflow), 4, 12),
                       thesis=f"net USDC inflow of {inflow:.0f} into related "
                              f"addresses precedes accumulation",
                       evidence=lines,
                       objections=["inflow is not market-directional on its own"])
    return Verdict("", Stance.ABSTAIN, abstain_reason="chain activity is neutral",
                   evidence=lines)


# --------------------------------------------------------------------------
# 4 — NEWS INTELLIGENCE
# --------------------------------------------------------------------------
def a04_news(ev: EvidenceState, ctx: dict) -> Verdict:
    n = ev.news
    rel = int(n.get("relevant") or 0)
    if rel == 0:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"{n.get('items', 0)} items captured, none "
                                      f"linked to this market above 0.2 relevance")
    direction = float(n.get("weighted_direction") or 0.0)
    mag = float(n.get("max_magnitude") or 0.0)
    latest = n.get("latest") or []
    confirmed = sum(1 for i in latest
                    if i.get("confirmation") in ("OFFICIAL", "MULTI_SOURCE"))
    lines = [f"{i.get('confirmation')}/{i.get('class')}: {i.get('title', '')[:70]}"
             for i in latest[:5]]
    if confirmed == 0:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"{rel} relevant items but none confirmed; "
                                      f"unconfirmed news is not fact",
                       evidence=lines)
    if abs(direction) < 0.05:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="confirmed news carries no directional "
                                      "implication",
                       evidence=lines)
    return Verdict("", Stance.FOR if direction > 0 else Stance.AGAINST,
                   confidence=linear_confidence(abs(direction) * mag, 0.02, 0.4),
                   thesis=f"reliability-weighted news direction {direction:+.3f} "
                          f"from {confirmed} confirmed source(s)",
                   evidence=lines)


# --------------------------------------------------------------------------
# 5 — EVENT ANALYSIS
# --------------------------------------------------------------------------
def a05_event(ev: EvidenceState, ctx: dict) -> Verdict:
    e = ev.events
    with_time = int(e.get("with_event_time") or 0)
    confirmed = int(e.get("confirmed") or 0)
    if with_time == 0:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no item carries an event timestamp, so "
                                      "publication lag cannot be measured")
    latest = ev.news.get("latest") or []
    lags = [i["publication_lag_secs"] for i in latest
            if i.get("publication_lag_secs") is not None]
    if not lags:
        return Verdict("", Stance.ABSTAIN, abstain_reason="no measurable lag")
    median_lag = statistics.median(lags)
    px_move = abs(float(ev.price.get("velocity_1h") or 0.0))
    lines = [f"median publication lag {median_lag:.0f}s over {len(lags)} items",
             f"price velocity since {px_move:+.4f}/h",
             f"confirmed items: {confirmed}"]
    # Fresh event, market has not moved -> the information may not be priced.
    if median_lag < 900 and px_move < 0.01:
        return Verdict("", Stance.FOR, confidence=0.45,
                       thesis="a recent confirmed event has not yet produced a "
                              "price response; the market may be un-adjusted",
                       evidence=lines,
                       objections=["the market may have correctly judged the "
                                   "event immaterial"])
    if median_lag < 900 and px_move > 0.05:
        return Verdict("", Stance.AGAINST, confidence=0.4,
                       thesis="the market has already moved on this event; "
                              "entering now pays for information already priced",
                       evidence=lines)
    return Verdict("", Stance.ABSTAIN,
                   abstain_reason="event timing carries no clear implication",
                   evidence=lines)


# --------------------------------------------------------------------------
# 6 — STATISTICAL RESEARCH
# --------------------------------------------------------------------------
def a06_statistical(ev: EvidenceState, ctx: dict) -> Verdict:
    s = ctx.get("strategy") or {}
    if not s:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no strategy record to evaluate")
    n = int(s.get("trade_count") or 0)
    p = s.get("p_value")
    thr = s.get("bh_threshold")
    denom = int(s.get("hypotheses_tested") or 0)
    lines = [f"n={n}", f"p={p}", f"BH threshold={thr}",
             f"hypotheses tested={denom}"]
    if not denom:
        return Verdict("", Stance.AGAINST, confidence=0.8,
                       thesis="no hypothesis denominator was recorded; the "
                              "false-discovery rate of this result is unknown",
                       evidence=lines,
                       objections=["a p-value without its search is not evidence"])
    if p is None or thr is None:
        return Verdict("", Stance.AGAINST, confidence=0.7,
                       thesis="no significance test on record", evidence=lines)
    if float(p) <= float(thr):
        return Verdict("", Stance.FOR,
                       confidence=linear_confidence(
                           math.log10(max(float(thr), 1e-12) / max(float(p), 1e-12)),
                           0.0, 2.0),
                       thesis=f"p={float(p):.4g} clears the pass-wide BH "
                              f"threshold {float(thr):.4g} over {denom} tests",
                       evidence=lines)
    return Verdict("", Stance.AGAINST, confidence=0.7,
                   thesis=f"p={float(p):.4g} does not clear BH {float(thr):.4g}",
                   evidence=lines)


# --------------------------------------------------------------------------
# 7 — BAYESIAN PROBABILITY
# --------------------------------------------------------------------------
def a07_bayesian(ev: EvidenceState, ctx: dict) -> Verdict:
    """Beta-Binomial posterior over the price-band base rate.

    The prior is the market-wide outcome rate in this price band, which on this
    dataset is materially different from the price itself (favourite-longshot
    bias). Using a uniform prior would throw that measurement away.
    """
    base = ctx.get("band_baseline") or {}
    n0 = int(base.get("n") or 0)
    if n0 < 200:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"price-band baseline has only {n0} "
                                      f"observations; the prior is unreliable")
    hit = float(base.get("hit_rate") or 0.0)
    mkt = float(ctx.get("market_probability") or ev.price.get("last") or 0.0)
    # Pseudo-counts, deliberately capped: 90 days of one venue should not
    # produce a prior so strong that live evidence cannot move it.
    strength = min(n0, 500)
    a = hit * strength + 1.0
    b = (1 - hit) * strength + 1.0
    post = a / (a + b)
    sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
    edge = post - mkt
    lines = [f"band prior hit rate {hit:.4f} over n={n0}",
             f"posterior {post:.4f} +/- {sd:.4f}",
             f"market {mkt:.4f}", f"edge {edge:+.4f}"]
    if abs(edge) < 2 * sd:
        return Verdict("", Stance.ABSTAIN, probability=post,
                       abstain_reason=f"edge {edge:+.4f} is inside 2 posterior "
                                      f"standard deviations ({2 * sd:.4f})",
                       evidence=lines)
    return Verdict("", Stance.FOR if edge > 0 else Stance.AGAINST,
                   confidence=linear_confidence(abs(edge) / max(sd, 1e-9), 2.0, 5.0),
                   probability=post,
                   thesis=f"Beta-Binomial posterior {post:.3f} against a market "
                          f"price of {mkt:.3f}",
                   evidence=lines,
                   objections=["the band prior is a market-wide effect, not an "
                               "edge over other participants"])


# --------------------------------------------------------------------------
# 8 — TIME-SERIES ANALYSIS
# --------------------------------------------------------------------------
def a08_timeseries(ev: EvidenceState, ctx: dict) -> Verdict:
    series = ctx.get("price_series") or []
    if len(series) < 30:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"{len(series)} prints is too short a "
                                      f"series for autocorrelation")
    px = [p for _, p in series]
    d = [b - a for a, b in zip(px, px[1:])]
    if len(d) < 20 or statistics.pstdev(d) == 0:
        return Verdict("", Stance.ABSTAIN, abstain_reason="degenerate series")
    m = statistics.fmean(d)
    var = statistics.pvariance(d)
    lag1 = sum((d[i] - m) * (d[i + 1] - m) for i in range(len(d) - 1)) \
        / ((len(d) - 1) * var)
    # 95% band for white noise
    band = 1.96 / math.sqrt(len(d))
    lines = [f"n_diffs={len(d)}", f"lag-1 autocorr={lag1:+.4f}",
             f"white-noise band=+/-{band:.4f}", f"drift={m:+.6f}/print"]
    if abs(lag1) < band:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"lag-1 autocorrelation {lag1:+.4f} is "
                                      f"inside the white-noise band; the series "
                                      f"carries no exploitable structure",
                       evidence=lines)
    if lag1 > 0:
        return Verdict("", Stance.FOR if m > 0 else Stance.AGAINST,
                       confidence=linear_confidence(abs(lag1), band, 0.4),
                       thesis=f"positive lag-1 autocorrelation ({lag1:+.3f}) "
                              f"means moves persist; drift is {m:+.5f}/print",
                       evidence=lines)
    return Verdict("", Stance.AGAINST if m > 0 else Stance.FOR,
                   confidence=linear_confidence(abs(lag1), band, 0.4),
                   thesis=f"negative lag-1 autocorrelation ({lag1:+.3f}) means "
                          f"moves revert; the recent drift is likely to unwind",
                   evidence=lines)


# --------------------------------------------------------------------------
# 9 — SEQUENCE / ORDER ANALYSIS
# --------------------------------------------------------------------------
def a09_sequence(ev: EvidenceState, ctx: dict) -> Verdict:
    """Markov / entropy structure in the direction sequence.

    Tests whether up/down prints are conditionally dependent. Explicitly does
    NOT assume randomness is predictable: the null is independence, and a
    chi-square below the critical value returns an abstention, not a weak
    signal.
    """
    series = ctx.get("price_series") or []
    if len(series) < 40:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"{len(series)} prints is too short for a "
                                      f"transition matrix")
    px = [p for _, p in series]
    sym = ["U" if b > a else ("D" if b < a else "F")
           for a, b in zip(px, px[1:])]
    sym = [s for s in sym if s != "F"]
    if len(sym) < 30:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="too few directional prints")
    trans = {"UU": 0, "UD": 0, "DU": 0, "DD": 0}
    for a, b in zip(sym, sym[1:]):
        trans[a + b] += 1
    n = sum(trans.values())
    nu = sym.count("U")
    p_u = nu / len(sym)
    # Chi-square for independence on the 2x2 transition table.
    chi = 0.0
    for a in "UD":
        row = trans[a + "U"] + trans[a + "D"]
        if row == 0:
            continue
        for b, p in (("U", p_u), ("D", 1 - p_u)):
            exp = row * p
            if exp > 0:
                chi += (trans[a + b] - exp) ** 2 / exp
    lines = [f"n_transitions={n}", f"P(U)={p_u:.3f}",
             f"transitions={trans}", f"chi2={chi:.2f} (crit 3.84 @ df=1)"]
    if chi < 3.84:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"chi2={chi:.2f} below the 3.84 critical "
                                      f"value; the sequence is indistinguishable "
                                      f"from independent",
                       evidence=lines)
    p_uu = trans["UU"] / max(trans["UU"] + trans["UD"], 1)
    last = sym[-1]
    cont = p_uu if last == "U" else 1 - (trans["DU"] /
                                         max(trans["DU"] + trans["DD"], 1))
    return Verdict("", Stance.FOR if (last == "U") == (cont > 0.5)
                   else Stance.AGAINST,
                   confidence=linear_confidence(chi, 3.84, 20.0) * 0.7,
                   thesis=f"transition structure is non-independent "
                          f"(chi2={chi:.1f}); P(continue|{last})={cont:.3f}",
                   evidence=lines,
                   objections=["in-sample structure in a short sequence "
                               "frequently fails out-of-sample"])


# --------------------------------------------------------------------------
# 10 — CROSS-MARKET ARBITRAGE
# --------------------------------------------------------------------------
def a10_cross_market(ev: EvidenceState, ctx: dict) -> Verdict:
    sibs = ev.related_markets.get("siblings") or []
    prices = ctx.get("sibling_prices") or {}
    if len(sibs) < 1 or not prices:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"{len(sibs)} sibling markets known, "
                                      f"{len(prices)} priced")
    mkt = float(ctx.get("market_probability") or 0.0)
    total = mkt + sum(prices.values())
    lines = [f"this market {mkt:.4f}"] + \
            [f"{k[:14]} {v:.4f}" for k, v in list(prices.items())[:6]] + \
            [f"sum={total:.4f} across {len(prices) + 1} outcomes"]
    # Mutually exclusive and exhaustive outcomes must sum to 1. A sum far from
    # 1 is either a real dislocation or evidence the outcomes are not actually
    # exclusive — and we cannot tell which from price alone.
    if abs(total - 1.0) < 0.03:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"outcome prices sum to {total:.3f}; "
                                      f"internally consistent",
                       evidence=lines)
    if total > 1.03:
        return Verdict("", Stance.AGAINST,
                       confidence=linear_confidence(total - 1.0, 0.03, 0.15) * 0.6,
                       thesis=f"outcomes sum to {total:.3f} > 1; this side is "
                              f"collectively overpriced",
                       evidence=lines,
                       objections=["the outcome set may not be exhaustive, in "
                                   "which case a sum above 1 is expected"])
    return Verdict("", Stance.FOR,
                   confidence=linear_confidence(1.0 - total, 0.03, 0.15) * 0.6,
                   thesis=f"outcomes sum to {total:.3f} < 1; this side is "
                          f"collectively underpriced",
                   evidence=lines,
                   objections=["the outcome set may be incomplete, or the "
                               "sibling markets may be stale"])


# --------------------------------------------------------------------------
# 11 — TOP-WALLET REPLICATION
# --------------------------------------------------------------------------
def a11_replication(ev: EvidenceState, ctx: dict) -> Verdict:
    copy = ctx.get("copy_score") or {}
    if not copy:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no COPY_SCORE computed for this candidate")
    score = float(copy.get("score") or 0.0)
    comps = copy.get("components") or {}
    lines = [f"{k}={v:+.3f}" for k, v in comps.items()]
    blockers = copy.get("blockers") or []
    if blockers:
        return Verdict("", Stance.AGAINST, confidence=0.6,
                       thesis="wallet replication is blocked: " + "; ".join(blockers),
                       evidence=lines, objections=blockers)
    if score < 0.4:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"COPY_SCORE {score:.3f} below the 0.40 "
                                      f"action floor",
                       evidence=lines)
    return Verdict("", Stance.FOR, confidence=linear_confidence(score, 0.4, 0.9),
                   thesis=f"COPY_SCORE {score:.3f}: the conditional evidence "
                          f"supports replicating this wallet's exposure here",
                   evidence=lines,
                   objections=["copying exposure logic is not copying notional; "
                               "the wallet's edge may depend on size we cannot "
                               "deploy"])


# --------------------------------------------------------------------------
# 12 — CONTRARIAN ANALYST
# --------------------------------------------------------------------------
def a12_contrarian(ev: EvidenceState, ctx: dict) -> Verdict:
    """Paid to look for the crowded side.

    Deliberately reads the same wallet layer as Agent 2 and draws the opposite
    inference from concentration. When both fire, the disagreement is real
    information about how one-sided the flow is.
    """
    top = ev.top_wallet_signals
    hhi = float(top.get("herfindahl") or 0.0)
    share = float(top.get("top_wallet_share") or 0.0)
    conv = float(ev.cross_wallet.get("convergence") or 0.0)
    lines = [f"herfindahl={hhi:.4f}", f"top wallet share={share:.3f}",
             f"price convergence={conv:.3f}", f"n_wallets={top.get('n_wallets')}"]
    if hhi > 0.5:
        return Verdict("", Stance.AGAINST, confidence=linear_confidence(hhi, 0.5, 0.9),
                       thesis=f"one wallet is {share:.0%} of the flow. This is "
                              f"not consensus, it is a single opinion, and "
                              f"following it concentrates our risk on theirs",
                       evidence=lines)
    if conv > 0.9 and int(top.get("n_wallets") or 0) >= 8:
        return Verdict("", Stance.AGAINST, confidence=0.45,
                       thesis="unusually tight agreement among many wallets is "
                              "a crowded trade; the marginal buyer is gone",
                       evidence=lines,
                       objections=["tight agreement can also mean the outcome "
                                   "is genuinely near-certain"])
    return Verdict("", Stance.ABSTAIN,
                   abstain_reason="flow is neither concentrated nor crowded",
                   evidence=lines)


# --------------------------------------------------------------------------
# 13 — RISK MANAGER
# --------------------------------------------------------------------------
def a13_risk(ev: EvidenceState, ctx: dict) -> Verdict:
    sz = ctx.get("sizing")
    acct = ctx.get("account")
    if sz is None or acct is None:
        return Verdict("", Stance.ABSTAIN, abstain_reason="no sizing or account")
    lines = [f"size=${sz.size_usdc:.2f}", f"max_loss=${sz.max_loss:.2f}",
             f"EV=${sz.expected_value:+.4f}", f"equity=${acct.equity:.2f}",
             f"drawdown={acct.drawdown:.1%}",
             f"open={acct.open_positions}"]
    if not sz.ok:
        return Verdict("", Stance.AGAINST, confidence=0.9,
                       thesis=f"not sizeable: {sz.reason}", evidence=lines,
                       objections=[sz.reason])
    loss_frac = sz.max_loss / acct.equity if acct.equity > 0 else 1.0
    if sz.expected_value <= 0:
        return Verdict("", Stance.AGAINST, confidence=0.9,
                       thesis=f"expected value is {sz.expected_value:+.4f} after "
                              f"costs", evidence=lines)
    if loss_frac > 0.05:
        return Verdict("", Stance.AGAINST, confidence=0.7,
                       thesis=f"a single loss costs {loss_frac:.1%} of the "
                              f"account", evidence=lines)
    # EV per unit of capital at risk — the number that actually matters at $100.
    ratio = sz.expected_value / max(sz.max_loss, 1e-9)
    return Verdict("", Stance.FOR if ratio > 0.02 else Stance.ABSTAIN,
                   confidence=linear_confidence(ratio, 0.02, 0.25),
                   thesis=f"EV/risk = {ratio:.3f} at {loss_frac:.1%} of equity",
                   evidence=lines,
                   abstain_reason="" if ratio > 0.02 else
                   f"EV/risk {ratio:.3f} is below the 0.02 floor")


# --------------------------------------------------------------------------
# 14 — EXECUTION SPECIALIST
# --------------------------------------------------------------------------
def a14_execution(ev: EvidenceState, ctx: dict) -> Verdict:
    ex = ev.execution
    sz = ctx.get("sizing")
    unc = ex.get("uncertainty") or []
    lines = [f"reference={ex.get('reference_price')}",
             f"spread={ex.get('spread')} measured={ex.get('spread_measured')}",
             f"assumed slippage={ex.get('assumed_slippage_bps')}bps",
             f"latency={ex.get('latency_ms')}ms"]
    if unc:
        return Verdict("", Stance.AGAINST, confidence=0.55,
                       thesis="execution cannot be modelled honestly: "
                              + ", ".join(unc),
                       evidence=lines, objections=unc)
    if sz is not None and sz.fill_probability < 0.7:
        return Verdict("", Stance.AGAINST, confidence=0.5,
                       thesis=f"modelled fill probability is only "
                              f"{sz.fill_probability:.0%}", evidence=lines)
    return Verdict("", Stance.FOR, confidence=0.5,
                   thesis="fill is modellable at a measured price with a "
                          "measured spread", evidence=lines)


# --------------------------------------------------------------------------
# 15 — ADVERSARIAL RED TEAM
# --------------------------------------------------------------------------
def a15_red_team(ev: EvidenceState, ctx: dict) -> Verdict:
    """Tries to kill the trade. Defaults to killing it when unsure.

    Asymmetric on purpose: a false kill costs one missed opportunity, which
    `learning/missed.py` will find and report. A false pass costs capital, and
    at $100 a handful of those ends the experiment.
    """
    objections: list[str] = []
    s = ctx.get("strategy") or {}
    sz = ctx.get("sizing")
    fair = float(ctx.get("fair_probability") or 0.0)
    mkt = float(ctx.get("market_probability") or 0.0)

    if ev.completeness < 0.5:
        objections.append(
            f"only {ev.completeness:.0%} of the information environment is "
            f"available; the thesis rests on what we could not see")
    if not ev.order_book.ok:
        objections.append("no order book: depth, spread and queue position are "
                          "assumptions, not measurements")
    if not ev.news.ok:
        objections.append("no news layer: an information shock could already be "
                          "in progress and we would not know")
    if abs(fair - mkt) > 0.25:
        objections.append(
            f"we claim the market is wrong by {abs(fair - mkt):.0%}. On a "
            f"liquid market that size of disagreement usually means our model "
            f"is wrong, not the market")
    if s and int(s.get("trade_count") or 0) < 50:
        objections.append(f"strategy has only {s.get('trade_count')} fills of "
                          f"evidence")
    if s and float(s.get("win_rate") or 0) >= 0.98:
        objections.append("a near-perfect win rate is a sampling artefact until "
                          "proven otherwise")
    if sz is not None and sz.detail.get("reduced_by_liquidity"):
        objections.append("the order had to be shrunk to fit available "
                          "liquidity; the edge may not survive at size")
    regime = ev.regime.get("primary") if ev.regime.ok else None
    if regime in ("PANIC", "INFORMATION_SHOCK"):
        objections.append(f"regime is {regime}; historical relationships are "
                          f"least reliable exactly here")
    if ev.regime.ok and float(ev.regime.get("confidence") or 0) < 0.5:
        objections.append("regime classification itself is low-confidence")

    # Two or more independent objections kill it.
    killed = len(objections) >= 2
    return Verdict("", Stance.AGAINST if killed else Stance.ABSTAIN,
                   confidence=clamp(0.3 + 0.15 * len(objections)) if killed else 0.0,
                   thesis=("red team kills this trade" if killed else
                           "red team found no decisive objection"),
                   objections=objections,
                   abstain_reason="" if killed else
                   f"only {len(objections)} objection(s); below the kill "
                   f"threshold of 2",
                   evidence=[f"{len(objections)} objections raised"])


# --------------------------------------------------------------------------
# 16 — OVERFITTING DETECTOR
# --------------------------------------------------------------------------
def a16_overfitting(ev: EvidenceState, ctx: dict) -> Verdict:
    s = ctx.get("strategy") or {}
    if not s:
        return Verdict("", Stance.ABSTAIN, abstain_reason="no strategy record")
    flags = []
    n = int(s.get("trade_count") or 0)
    params = int(s.get("n_params") or 0)
    tested = int(s.get("hypotheses_tested") or 0)
    is_exp = float(s.get("is_expectancy") or 0.0)
    oos_exp = float(s.get("oos_expectancy") or s.get("expectancy") or 0.0)
    if params and n and n / max(params, 1) < 20:
        flags.append(f"{n} fills for {params} parameters ({n / params:.0f} per "
                     f"parameter; 20 is the floor)")
    if is_exp > 0 and oos_exp < is_exp * 0.5:
        flags.append(f"out-of-sample expectancy {oos_exp:+.4f} is less than half "
                     f"the in-sample {is_exp:+.4f}")
    if tested > 1000 and n < 100:
        flags.append(f"{tested} hypotheses tested against only {n} fills")
    if float(s.get("perturbation_survival") or 1.0) < 0.5:
        flags.append("fails under small parameter perturbation")
    if float(s.get("concentration") or 0.0) > 0.6:
        flags.append(f"{float(s['concentration']):.0%} of profit comes from one "
                     f"market")
    lines = [f"n={n}", f"params={params}", f"tested={tested}",
             f"IS exp={is_exp:+.4f}", f"OOS exp={oos_exp:+.4f}"]
    if flags:
        return Verdict("", Stance.AGAINST,
                       confidence=clamp(0.35 + 0.15 * len(flags)),
                       thesis="overfitting indicators present",
                       evidence=lines, objections=flags)
    return Verdict("", Stance.FOR, confidence=0.4,
                   thesis="no overfitting indicator triggered", evidence=lines)


# --------------------------------------------------------------------------
# 17 — DATA QUALITY AUDITOR
# --------------------------------------------------------------------------
def a17_data_quality(ev: EvidenceState, ctx: dict) -> Verdict:
    issues = []
    for l in ev.layers():
        if l.availability.value == "STALE":
            issues.append(f"{l.name} is {l.age_secs}s stale")
        if l.ok and l.rows == 0:
            issues.append(f"{l.name} reports OK with zero rows")
    if ev.price.ok and ev.order_book.ok:
        last = float(ev.price.get("last") or 0)
        mid = float(ev.order_book.get("mid") or 0)
        if mid and abs(last - mid) > 0.05:
            issues.append(f"last print {last:.3f} disagrees with book mid "
                          f"{mid:.3f} by {abs(last - mid):.3f}")
    covered = len(ev.available_layers())
    lines = [f"{covered}/{len(ev.layers())} layers usable"] + issues[:6]
    if issues:
        return Verdict("", Stance.AGAINST, confidence=clamp(0.3 + 0.2 * len(issues)),
                       thesis="data quality problems present",
                       evidence=lines, objections=issues)
    if ev.completeness < 0.5:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"data is clean but only "
                                      f"{ev.completeness:.0%} complete",
                       evidence=lines)
    return Verdict("", Stance.FOR, confidence=0.45,
                   thesis="no data-quality anomaly detected", evidence=lines)


# --------------------------------------------------------------------------
# 18 — REGIME DETECTOR
# --------------------------------------------------------------------------
def a18_regime(ev: EvidenceState, ctx: dict) -> Verdict:
    r = ev.regime
    primary = r.get("primary")
    conf = float(r.get("confidence") or 0.0)
    s = ctx.get("strategy") or {}
    allowed = s.get("regimes") or []
    lines = [f"regime={primary}", f"flags={r.get('flags')}",
             f"classification confidence={conf:.2f}",
             f"strategy regimes={allowed or 'unrestricted'}"]
    if not r.ok:
        return Verdict("", Stance.ABSTAIN, abstain_reason="regime unknown")
    if allowed and primary not in allowed:
        return Verdict("", Stance.AGAINST, confidence=0.75,
                       thesis=f"strategy was validated in {allowed} but the "
                              f"current regime is {primary}",
                       evidence=lines,
                       objections=[f"regime mismatch: {primary}"])
    if primary in ("PANIC", "INFORMATION_SHOCK", "INFORMATION_VACUUM"):
        return Verdict("", Stance.AGAINST, confidence=0.5,
                       thesis=f"{primary} is a regime where measured "
                              f"relationships are least dependable",
                       evidence=lines)
    return Verdict("", Stance.FOR, confidence=conf * 0.6,
                   thesis=f"regime {primary} is compatible with the strategy",
                   evidence=lines)


# --------------------------------------------------------------------------
# 19 — PORTFOLIO OPTIMIZER
# --------------------------------------------------------------------------
def a19_portfolio(ev: EvidenceState, ctx: dict) -> Verdict:
    acct = ctx.get("account")
    sz = ctx.get("sizing")
    if acct is None or sz is None:
        return Verdict("", Stance.ABSTAIN, abstain_reason="no account or sizing")
    risk = ev.risk
    by_corr = risk.get("by_correlation") or {}
    ck = ctx.get("correlation_key") or ""
    existing = float(by_corr.get(ck, 0.0))
    post = existing + sz.size_usdc
    eq = acct.equity or 1e-9
    lines = [f"correlation bucket {ck[:24]}: ${existing:.2f} -> ${post:.2f}",
             f"= {post / eq:.1%} of ${eq:.2f} equity",
             f"gross exposure ${risk.get('gross_exposure', 0):.2f}",
             f"open positions {risk.get('open_positions', 0)}"]
    limit = eq * 0.25
    if post > limit:
        return Verdict("", Stance.AGAINST, confidence=0.8,
                       thesis=f"this fill takes correlated exposure to "
                              f"${post:.2f}, past the ${limit:.2f} bucket cap",
                       evidence=lines,
                       objections=["correlated concentration"])
    if existing > 0:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"we already hold ${existing:.2f} of this "
                                      f"underlying; adding is not diversifying",
                       evidence=lines)
    return Verdict("", Stance.FOR, confidence=0.4,
                   thesis="adds a genuinely uncorrelated bucket to the book",
                   evidence=lines)


# --------------------------------------------------------------------------
# 20 — POST-TRADE FORENSICS
# --------------------------------------------------------------------------
def a20_post_trade(ev: EvidenceState, ctx: dict) -> Verdict:
    """Applies what previous losses taught, to this candidate.

    This is the loop closing: `learning/forensics.py` classifies every loss and
    records which strategy, feature and regime were implicated. Here those
    records become a veto on repeating the same mistake.
    """
    lessons = ctx.get("loss_lessons") or []
    if not lessons:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no prior loss forensics on record")
    sid = (ctx.get("strategy") or {}).get("strategy_id")
    regime = ev.regime.get("primary") if ev.regime.ok else None
    hits = [l for l in lessons
            if (l.get("strategy_id") == sid and sid)
            or (l.get("regime") and l.get("regime") == regime)]
    lines = [f"{l.get('classification')}: {str(l.get('narrative'))[:70]}"
             for l in hits[:5]]
    if not hits:
        return Verdict("", Stance.FOR, confidence=0.3,
                       thesis=f"no recorded loss matches this strategy or the "
                              f"{regime} regime",
                       evidence=[f"{len(lessons)} loss records checked"])
    repeated = [h for h in hits if h.get("remedy") in ("retire", "risk")]
    if repeated:
        return Verdict("", Stance.AGAINST,
                       confidence=clamp(0.4 + 0.15 * len(repeated)),
                       thesis=f"{len(repeated)} prior loss(es) in this exact "
                              f"configuration led to a risk or retirement remedy",
                       evidence=lines,
                       objections=[h.get("classification", "") for h in repeated])
    return Verdict("", Stance.ABSTAIN,
                   abstain_reason=f"{len(hits)} related losses on record but "
                                  f"none produced a blocking remedy",
                   evidence=lines)


# --------------------------------------------------------------------------
# 21 — META-RESEARCHER
# --------------------------------------------------------------------------
def a21_meta(ev: EvidenceState, ctx: dict) -> Verdict:
    """Judges the research process, not the trade.

    Asks whether the evidence supporting this candidate was produced by a
    search whose size we know. If the denominator is unknown, everything
    downstream is uninterpretable regardless of how good it looks.
    """
    p = ctx.get("pass_stats") or {}
    if not p:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no research pass statistics available")
    tested = int(p.get("tested") or 0)
    distinct = int(p.get("distinct_tested") or 0)
    surviving = int(p.get("surviving") or 0)
    lines = [f"tested={tested}", f"distinct={distinct}",
             f"surviving={surviving}",
             f"BH threshold={p.get('bh_threshold')}"]
    if tested == 0:
        return Verdict("", Stance.AGAINST, confidence=0.7,
                       thesis="the search space is unrecorded", evidence=lines)
    # Inert axes inflate the multiple-testing penalty for nothing. V2 measured
    # this on the same data: 5,184 transformations of which ~432 were distinct,
    # making the BH threshold ~12x stricter than the evidence required.
    if distinct and tested / distinct > 4:
        return Verdict("", Stance.AGAINST, confidence=0.45,
                       thesis=f"{tested} tests collapse to only {distinct} "
                              f"distinct ones ({tested / distinct:.0f}x "
                              f"redundancy); the correction is paying for "
                              f"search that never happened",
                       evidence=lines,
                       objections=["inert search axes inflate the FDR penalty"])
    rate = surviving / tested
    if rate > 0.5:
        return Verdict("", Stance.AGAINST, confidence=0.5,
                       thesis=f"{rate:.0%} of hypotheses survived. A search that "
                              f"confirms most of what it tests is not testing "
                              f"anything",
                       evidence=lines)
    return Verdict("", Stance.FOR, confidence=0.4,
                   thesis=f"{surviving}/{tested} survived; the denominator is "
                          f"recorded and the survival rate is plausible",
                   evidence=lines)


# --------------------------------------------------------------------------
# 22 — STRATEGY DISCOVERY
# --------------------------------------------------------------------------
def a22_discovery(ev: EvidenceState, ctx: dict) -> Verdict:
    """Looks for a structural pattern here worth turning into a hypothesis.

    Never votes FOR a trade — discovery is not permission. It votes ABSTAIN
    with a proposal attached, which the research loop picks up.
    """
    proposals = []
    if ev.regime.ok:
        r = ev.regime.get("primary")
        m = ev.regime.get("measurements") or {}
        if r == "LOW_LIQUIDITY" and abs(float(m.get("velocity_1h") or 0)) > 0.03:
            proposals.append("price moves sharply in low-liquidity conditions "
                             "here; test whether such moves revert within 6h")
    if ev.top_wallet_signals.ok:
        hhi = float(ev.top_wallet_signals.get("herfindahl") or 0)
        if hhi > 0.4:
            proposals.append("flow is highly concentrated; test whether the "
                             "dominant wallet's entries predict 24h returns")
    if ev.related_markets.ok and ev.related_markets.rows > 2:
        proposals.append(f"{ev.related_markets.rows} sibling markets exist; test "
                         f"outcome-sum consistency as a standing signal")
    if not proposals:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no structural pattern worth a hypothesis")
    return Verdict("", Stance.ABSTAIN,
                   abstain_reason="discovery proposes hypotheses; it never "
                                  "authorises a trade",
                   thesis=f"{len(proposals)} hypothesis proposal(s)",
                   evidence=proposals)


# --------------------------------------------------------------------------
# 23 — STRATEGY RETIREMENT
# --------------------------------------------------------------------------
def a23_retirement(ev: EvidenceState, ctx: dict) -> Verdict:
    s = ctx.get("strategy") or {}
    if not s:
        return Verdict("", Stance.ABSTAIN, abstain_reason="no strategy record")
    status = s.get("status")
    recent = float(s.get("recent_expectancy") or 0.0)
    lifetime = float(s.get("expectancy") or 0.0)
    dd = float(s.get("max_drawdown") or 0.0)
    lines = [f"status={status}", f"lifetime expectancy={lifetime:+.4f}",
             f"recent expectancy={recent:+.4f}", f"max drawdown={dd:.1%}"]
    if status in ("DEGRADED", "SUSPENDED", "RETIRED"):
        return Verdict("", Stance.AGAINST, confidence=0.95,
                       thesis=f"strategy is {status}", evidence=lines,
                       objections=[f"status {status}"])
    if lifetime > 0 and recent < 0:
        return Verdict("", Stance.AGAINST, confidence=0.65,
                       thesis="recent expectancy has turned negative while "
                              "lifetime remains positive — the classic "
                              "degradation signature",
                       evidence=lines,
                       objections=["strategy degrading"])
    if dd > 0.3:
        return Verdict("", Stance.AGAINST, confidence=0.5,
                       thesis=f"max drawdown {dd:.0%} exceeds tolerance",
                       evidence=lines)
    return Verdict("", Stance.FOR, confidence=0.35,
                   thesis="no degradation signature", evidence=lines)


# --------------------------------------------------------------------------
# 24 — PERFORMANCE ATTRIBUTION
# --------------------------------------------------------------------------
def a24_attribution(ev: EvidenceState, ctx: dict) -> Verdict:
    attr = ctx.get("attribution") or {}
    if not attr:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="no attribution history yet")
    contrib = attr.get("by_source") or {}
    lines = [f"{k}: {v:+.4f}" for k, v in
             sorted(contrib.items(), key=lambda kv: -abs(kv[1]))[:6]]
    src = ctx.get("primary_signal_source") or ""
    if src and src in contrib:
        c = float(contrib[src])
        if c < 0:
            return Verdict("", Stance.AGAINST, confidence=0.55,
                           thesis=f"the signal source driving this candidate "
                                  f"({src}) has contributed {c:+.4f} historically",
                           evidence=lines, objections=[f"{src} is net negative"])
        return Verdict("", Stance.FOR, confidence=linear_confidence(c, 0.0, 0.1),
                       thesis=f"{src} has contributed {c:+.4f} historically",
                       evidence=lines)
    return Verdict("", Stance.ABSTAIN,
                   abstain_reason=f"no attribution history for source '{src}'",
                   evidence=lines)


# --------------------------------------------------------------------------
# 25 — INFORMATION FUSION
# --------------------------------------------------------------------------
def a25_fusion(ev: EvidenceState, ctx: dict) -> Verdict:
    """Judges the evidence state as a whole rather than any one layer.

    Runs last conceptually: it asks whether the independent channels corroborate
    each other, which is a different question from whether any of them is
    individually convincing.
    """
    channels = {
        "wallet": ev.wallets.ok,
        "news": ev.news.ok,
        "chain": ev.blockchain.ok,
        "book": ev.order_book.ok,
        "cross_market": ev.related_markets.ok,
        "price": ev.price.ok,
    }
    live = [k for k, v in channels.items() if v]
    lines = [f"channels live: {', '.join(live) or 'none'}",
             f"completeness {ev.completeness:.0%}"]
    if len(live) < 3:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason=f"only {len(live)} independent channel(s) "
                                      f"live; corroboration is not possible",
                       evidence=lines)
    # Direction from each channel that has one.
    dirs = []
    if ev.news.ok:
        dirs.append(("news", float(ev.news.get("weighted_direction") or 0.0)))
    if ev.price.ok:
        dirs.append(("price", float(ev.price.get("velocity_1h") or 0.0) * 10))
    dna = ctx.get("wallet_dna") or {}
    if ev.wallets.ok and dna:
        tops = ev.wallets.get("top") or []
        a = [float(dna[t["wallet"]].get("alpha_vs_band") or 0.0)
             for t in tops if t["wallet"] in dna]
        if a:
            dirs.append(("wallet", statistics.fmean(a) * 5))
    signed = [d for _, d in dirs if abs(d) > 0.02]
    lines += [f"{k}={v:+.3f}" for k, v in dirs]
    if len(signed) < 2:
        return Verdict("", Stance.ABSTAIN,
                       abstain_reason="fewer than two channels carry a "
                                      "directional view",
                       evidence=lines)
    if all(d > 0 for d in signed) or all(d < 0 for d in signed):
        return Verdict("", Stance.FOR if signed[0] > 0 else Stance.AGAINST,
                       confidence=clamp(0.3 + 0.15 * len(signed)),
                       thesis=f"{len(signed)} independent channels agree on "
                              f"direction",
                       evidence=lines)
    return Verdict("", Stance.AGAINST, confidence=0.4,
                   thesis="independent channels contradict each other; "
                          "confidence must fall, not average out",
                   evidence=lines,
                   objections=["channel contradiction"])


# --------------------------------------------------------------------------
AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(1, "MARKET_MICROSTRUCTURE", "order flow and price dynamics",
              ("price", "liquidity"), a01_microstructure),
    AgentSpec(2, "WALLET_FORENSICS", "who is trading and whether they have alpha",
              ("wallets",), a02_wallet_forensics),
    AgentSpec(3, "BLOCKCHAIN_FORENSICS", "on-chain capital movement",
              ("blockchain",), a03_blockchain),
    AgentSpec(4, "NEWS_INTELLIGENCE", "confirmed information and its direction",
              ("news",), a04_news),
    AgentSpec(5, "EVENT_ANALYSIS", "event timing versus market response",
              ("events", "price"), a05_event),
    AgentSpec(6, "STATISTICAL_RESEARCH", "significance against the search size",
              (), a06_statistical),
    AgentSpec(7, "BAYESIAN_PROBABILITY", "posterior over the price-band prior",
              ("price",), a07_bayesian),
    AgentSpec(8, "TIME_SERIES", "autocorrelation and drift", ("price",),
              a08_timeseries),
    AgentSpec(9, "SEQUENCE_ANALYSIS", "Markov structure in print direction",
              ("price",), a09_sequence),
    AgentSpec(10, "CROSS_MARKET", "outcome-sum consistency across siblings",
              ("related_markets",), a10_cross_market),
    AgentSpec(11, "WALLET_REPLICATION", "conditional COPY_SCORE", (),
              a11_replication),
    AgentSpec(12, "CONTRARIAN", "is this side crowded",
              ("top_wallet_signals", "cross_wallet"), a12_contrarian,
              adversarial=True),
    AgentSpec(13, "RISK_MANAGER", "loss size against the bankroll", ("risk",),
              a13_risk),
    AgentSpec(14, "EXECUTION_SPECIALIST", "can this actually be filled",
              ("execution",), a14_execution),
    AgentSpec(15, "RED_TEAM", "actively tries to kill the trade", (),
              a15_red_team, adversarial=True),
    AgentSpec(16, "OVERFITTING_DETECTOR", "is the evidence an artefact", (),
              a16_overfitting, adversarial=True),
    AgentSpec(17, "DATA_QUALITY_AUDITOR", "do the inputs contradict themselves",
              (), a17_data_quality, adversarial=True),
    AgentSpec(18, "REGIME_DETECTOR", "does the strategy belong in this regime",
              ("regime",), a18_regime),
    AgentSpec(19, "PORTFOLIO_OPTIMIZER", "marginal effect on the book",
              ("risk",), a19_portfolio),
    AgentSpec(20, "POST_TRADE_FORENSICS", "have we lost this way before", (),
              a20_post_trade, adversarial=True),
    AgentSpec(21, "META_RESEARCHER", "is the research process sound", (),
              a21_meta, adversarial=True),
    AgentSpec(22, "STRATEGY_DISCOVERY", "propose new hypotheses", (),
              a22_discovery),
    AgentSpec(23, "STRATEGY_RETIREMENT", "has this strategy degraded", (),
              a23_retirement, adversarial=True),
    AgentSpec(24, "PERFORMANCE_ATTRIBUTION", "has this signal source paid", (),
              a24_attribution),
    AgentSpec(25, "INFORMATION_FUSION", "do independent channels corroborate",
              ("price",), a25_fusion),
)

BY_NAME = {a.name: a for a in AGENTS}
ADVERSARIAL = tuple(a for a in AGENTS if a.adversarial)


def catalogue() -> list[dict]:
    return [{"number": a.number, "name": a.name, "role": a.role,
             "requires": list(a.requires), "adversarial": a.adversarial}
            for a in AGENTS]
