# What this system cannot answer, and why

Stated plainly rather than buried, because the fastest way to lose money with a
quantitative system is to act on a number whose uncertainty nobody wrote down.

Everything below is a property of the **available data**, not of the code. Each
entry says what would fix it, and each is reported by the running system rather
than only documented here.

## 1. There is no historical order book

**Breaks:** depth, spread, partial fills, queue position, market impact,
iceberg detection, absorption, and any microstructural event shorter than a
poll interval.

**How V3 handles it.** `ev.order_book.availability` is `UNAVAILABLE`, never
`OK` with zeros. `EXECUTION_VALIDITY` lists what could not be modelled on every
fill and refuses outright in LIVE mode. `CapitalEngine.size` returns
`LIQUIDITY_INFEASIBLE` rather than assuming a fill, because unmeasured
liquidity is not infinite liquidity.

**Interim.** A tape-derived proxy: if anyone printed at time T, that is a price
you could plausibly have paid. Real and causal, but not continuous - good for
delay-decay comparison, not for depth. It is labelled `"proxy": true` in the
payload so no consumer can mistake it for a measurement.

**Fix.** `pqv3 collect --enable` starts the snapshotter. **History cannot be
backfilled.** Nothing recovers it.

## 2. `resolutions.settled_ts` is 0 in all 8,116 rows

The moment an outcome became public is recorded nowhere.

**Breaks:** point-in-time wallet track record. The fallback (`resolutions.ts`,
when V1 *observed* the resolution) is later than the trade in 100% of joined
rows, so it is **safe** - it can only delay information, never advance it - but
its range spans days, so older trades all appear to settle at once.

**Measured consequence.** Four search axes are structurally inert:
`min_settled_n`, `min_roll_win_rate`, `max_consec_losses`, `min_edge_t`. A
sweep still pays their multiple-testing cost, making the Benjamini-Hochberg
threshold roughly **12x stricter than the evidence requires**, for no benefit.

Any hypothesis of the form *"follow this wallet while it is running hot"* is
currently **untestable**. Not false - untestable. Do not let a backtest tell
you otherwise.

**Measured consequence #2, worse than originally documented.** It also breaks
the $100 capital simulation outright. All 64,041 out-of-sample positions appear
to settle inside the same 0.4 days, three days AFTER the tape ends, because V1
observed every resolution in one batch. A hold-to-resolution simulation frees
capital at settlement; if nothing ever settles, the account fills its
concurrent-position slots once and skips every later signal forever — so every
strategy reported exactly `max_open_positions` trades and an identical return.
`backtest.settlement_clock_quality()` now measures this and the simulation
falls back to an EXPLICIT modelled hold, labelled MODELLED everywhere it is
reported and refused as promotion evidence by the validation ladder.

**Fix.** `pqv3 collect --backfill-settled` asks the venue for real resolution
times (tier `VENUE_REPORTED`, confidence 1.00) and records tier-3
`FIRST_OBSERVED` going forward. Tiers are never blended: a fallback timestamp
does not count toward coverage, and `pit_features_enabled` stays false until
500 settlements carry confidence at or above 0.60.

## 3. No news, event or blockchain history

All three start empty and accumulate. Agents 3, 4 and 5 abstain rather than
concluding that nothing is happening. `NOT_CONFIGURED` is distinguished from
`UNAVAILABLE` so that "we never asked" is never mistaken for "we asked and
found nothing".

**Not implemented, and it matters:** news-to-market link *direction* is 0.0 by
design. Headline sentiment does not determine which side of a binary market
benefits, and a wrong sign is worse than no sign. Directional news signals
require per-market rules, not a sentiment score.

**Also not implemented:** chain events are stored unparsed. Decoding transfers,
splits, merges and redemptions needs the CTF and USDC ABIs and their topic
filters. Until that lands, Agent 3 sees counts rather than semantics.

## 4. The tape is one venue and 112 days

1,285 markets with settled outcomes is not many for a search that can generate
thousands of hypotheses. This is why the BH correction, the wallet-alpha
control and the red team matter more here than raw model quality: the dominant
risk is not a weak model, it is a strong-looking result from a small sample.

## 5. Early-exit results are MODELLED; settlement results are EXACT

Holding to resolution has an exactly known payoff (`resolution - price`, with
`resolution` in {0,1}). Every other exit is priced off tape prints, always in
the pessimistic direction: a target between two prints fills at the first print
*past* it, a stop can be jumped through, and a sparse token cannot support an
early exit at all. The two are not directly comparable and must not be ranked
against each other.

## 6. No strategy has been validated

`strategies` is empty. `VALIDATED` would mean *survived historical
out-of-sample validation*, which is not the same as *profitable*. The dashboard
reports zero, and `win_rate` renders as an em dash rather than `0%`, because an
untested rate is not a measured zero.

## 7. The scanner truncates, and says so

Stage 1 ranks by notional and takes the top 600 markets; stage 2 takes the top
25 to a full decision. Both cuts are reported in `ScanResult.notes` and the
dropped markets are recorded for missed-opportunity analysis. A cap nobody
reports reads as "we covered everything".
