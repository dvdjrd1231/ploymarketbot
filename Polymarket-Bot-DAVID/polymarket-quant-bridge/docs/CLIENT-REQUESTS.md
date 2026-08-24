# Client requests — 11 Aug 2026 session

Verbatim asks from David during the first Windows test, with status.

| # | What he asked | Status |
|---|---|---|
| 1 | *"Make sure it opens with a double click of a file instead of CMD prompt"* | ✅ Done — `START-DASHBOARD.vbs` opens the desktop app with no console window at all |
| 2 | *"Is there a dashboard like the other bridge?"* | ✅ Done — `pqb/gui/`, same PyQt6 desktop app style as `qc_lean_bridge` |
| 3 | *"I don't understand what I'm looking at"* | ✅ Addressed — the dashboard replaces raw log lines with plain-English panels |
| 4 | *"The bridge should analyze many wallets at once and create perfect trades for trading real anomalies"* | ✅ Already working — 1,818 wallets observed / 126 ranked in his own run; the dashboard now makes it visible |
| 5 | *"Would be best if the software knew that I was starting with a $100 in the account"* | ✅ `mode.paper_starting_balance: 100.0`, and live mode reads the real balance |
| 6 | *"Fees for each trade are around 1 cent or less typically"* | ✅ Done — `engine.portfolio.fee_per_trade_usdc`, charged on every simulated fill and enforced during sizing |
| 7 | *"I would like the strategy to compound profits"* | ✅ Already compounds; see the note below, which he should read |

---

## On fees — this one needs a conversation, not just a setting

A 1-cent fee is small in absolute terms and **large relative to his opening trade
size**. The progression starts at **$0.19**:

| Trade size | 1c fee, one way | Round trip (in + out) |
|---|---|---|
| **$0.19** (step 1) | 5.3% | **~10.5%** |
| $0.59 (step 5) | 1.7% | ~3.4% |
| $1.09 (step 11) | 0.9% | ~1.8% |
| $6.07 (step 50) | 0.16% | ~0.3% |

At step 1, a position must gain over **10%** just to break even. That is not a
reason to abandon the progression — it is his rule and it is implemented as
specified — but it does mean the early steps are the hardest part of the curve,
and any early results should be read with that in mind.

`engine.portfolio.min_order_usdc` is the lever if he wants to soften it: raising
it to, say, $2.00 puts the round-trip cost near 1% while still respecting the
progression as a *minimum*. **His call, not ours** — flagged, not changed.

## On compounding — already true, worth showing him

Position size is `portfolio_value × max_position_fraction`, so it grows with the
account automatically: as profits accumulate, every subsequent position is
larger. Nothing is withdrawn or held back.

On top of that his doubling rule steps the *minimum* size up a notch each time
the account doubles. So there are two compounding effects running together —
continuous (position size tracks equity) and stepped (the progression).

The Overview tab now shows portfolio value against the doubling baseline and
target, so he can watch both happen.

---

# Client requests — 12 Aug 2026 session (evening)

Verbatim asks from David during the WhatsApp exchange, with status.

| # | What he asked | Status |
|---|---|---|
| 8 | *"Each wallet is probably running multiple algorithm strategies… Sports market has an algorithm, then political market has its own algorithm, and crypto market has its own"* | ✅ Done — `pqb/analytics/segments.py` classifies every market; `pqb.cli strategies` fits a separate best exit rule per market category per wallet; `pqb.cli wallet 0x.. --by-market` breaks one wallet into its per-category strategies |
| 9 | *"Reverse engineer every top wallet… adjust the exits to give the optimal algorithm for every single ranked wallet"* | ✅ Extended — the playbook now runs per-segment across the top-25, ranked by return-minus-volatility |
| 10 | *"Some kind of time exit analysis after a spike… rides up then comes back down after a minute or two"* | ✅ Done — added trailing-stop and time-exit rule families to the playbook grid; both use real trade timestamps so the spike is captured, not round-tripped |
| 11 | *"The trades that are always successful fall between the $0.60–$0.79 range"* | ✅ Measurable — `wallet --by-market` already breaks each segment's P&L down by entry-price band, so this is checkable per wallet rather than by eye |
| 12 | *"That wallet 0x2005…875ea is a bot, thousands of positive trades"* | ⚠️ Partial — can be pointed at directly, but only analyses wallets it has observed; needs more backfill to see its full history |

## The finding that matters

Splitting by market immediately surfaced a wallet (`0x24c8…23e1`) running
**take-profit-at-+100% in economics but take-profit-at-+10% in sports** — two
different algorithms under one address, exactly as David predicted. Treated as
one strategy those averaged into noise.

## The honest caveat (see reply-to-david-5.txt §3)

Most per-market segments currently have only 3–7 settled positions, and several
read "100% win." At that sample size that means "hasn't lost yet," not "cannot
lose." Minimum is set to 4 positions and thin segments are flagged provisional.
`backfill --markets 300` is the fix — it widens every segment. Do not size up on
a 3-trade segment.

---

# Client requests — 12 Aug 2026 session (late afternoon)

| # | What he asked | Status |
|---|---|---|
| 13 | *"Reverse-engineer the lifecycle: why they enter, how they add, and what causes them to start buying the opposite outcome. Manage markets as inventory, not isolated bets."* | ✅ Done — `pqb/analytics/lifecycle.py` + `pqb.cli lifecycle 0x..` reconstructs entry → adds (avg up/down) → the flip into the opposite outcome, and whether the flip is defending a loser or locking a winner |
| 14 | *"Do not assume RN1 is 100% lossless — our cash-flow reconstruction contradicts it."* | ✅ Proven — the cash-flow block separates money returned from money still at risk; a real wallet showed **$23,958 of $40,599 (59%) still at risk**, i.e. the 100% win rate ignores unresolved losers |
| 15 | *"The 60–79¢ entry range is our strongest simple directional strategy."* | ✅ Measured — lifecycle breaks out the 0.60–0.79 band per wallet (n, win rate, return) so the claim is checkable, not eyeballed |
| 16 | *"Is it ready to test on my computer?"* | ✅ Yes for paper mode — 328 tests pass; still needs a forward run so its own predictions resolve before a calibration number can prove edge |
| 17 | *"Are you running this on Telegram? I don't want others to have access. At all."* | ✅ Answered — **No.** Grep-confirmed: no Telegram/Discord/web-server/webhook/listener anywhere in `pqb/`. Runs 100% locally, outbound-only to Polymarket's public data API. Nothing is exposed. |

---

# Client requests — 12–13 Aug 2026 (late night session)

| # | What he asked | Status |
|---|---|---|
| 18 | *"Per trade min 0.001%"* | ✅ Settings floor lowered to 0.001%; fixed the save bug that rounded tiny values to 0 ("it defaults back") |
| 19 | *"Most trades: as many as the bridge decides — no limit"* | ✅ Removed from settings; `max_open_positions: 0` = unlimited (`position_cap`) |
| 20 | *"Stop buying / always keep in cash shouldn't be there"* | ✅ Removed from settings; drawdown pause and cash reserve default 0 (off) — the decision engine owns risk |
| 21 | *"Check every 1 second"* | ✅ Floor lowered 5s → 1s |
| 22 | *"Does this study HISTORICAL closed trades or only new ones?"* | ✅ **He was right — discovery was live-capture only.** New `analytics/history_series.py`: settled markets' backfilled tapes replay into research series with known outcomes; `research` studies them immediately. First run: 12 series, 368 candidates, honest 0 accepted. Deep backfill (412 markets, 328k trades) widens the next pass. |
| 23 | Screenshot: *"Trading is halted — records disagree with the exchange"* in SIMULATION | ✅ Paper-mode reconcile mismatches now self-heal + log but never halt (there is no exchange in simulation); live halt behaviour unchanged + tested |
