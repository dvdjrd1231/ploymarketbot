# Decision-Layer Redesign — Reuse Map & Task Plan

Deliverable required by the redesign spec (§2, §11): for every capability the
spec names, state **reuse / extend / build-new** and why, then a task plan.
**No engine code is written until this is confirmed.**

Grounded in a fresh inventory of both repos on 2026-08-12 — not from memory:
- `polymarket-quant-bridge` (this repo) — source of truth for architecture.
- `../ploymarketbot` (note the spelling) — connectivity reference. Scanned in
  full: `backend/services/` holds all connectivity; there is **no on-chain /
  contract layer of any kind**, and it assumes **zero fees**.

Legend — **Reuse**: import/use as-is. **Extend**: build on an existing seam.
**Build-new**: no antecedent in either repo.

> **Honest framing.** This spec supersedes the earlier 3-phase decision-engine
> prompt, most of which is **already implemented** in this repo (EV engine,
> calibrated probability, Bayesian-shrinkage wallet quality, correlation/
> consensus de-dup, portfolio EV-replacement, lifecycle, segments, journal,
> kill switch, reconciliation). So most rows below are "already built — audit
> against the new wording," and the real work is a **small set of true gaps**,
> flagged **BUILD-NEW** and listed again in the task plan. The single largest is
> CTF/on-chain reconstruction (§6, acceptance 11.3).

---

## §3 — Non-negotiable constraints

| Constraint | Status | Where / what's missing |
|---|---|---|
| Secrets from env/keychain, never logged/committed/transmitted | **Reuse** | `pqb/config.py` (`${env:...}`, `_SECRET_HINTS`, `redacted()`), `.env` gitignored; upstream `SecretStore` (keyring/DPAPI/AES) available if we want OS-keychain parity. Verified no key is written to disk in this repo. |
| `DRY_RUN=true` default; live needs explicit flag **and** interactive confirmation | **Extend** | Two-flag gate exists (`mode.dry_run` + `mode.allow_live`, `mode.live` requires both). **Gap: interactive confirmation prompt on live start** is not yet enforced — add. |
| **Live-execution calibration gate — code-enforced** | **BUILD-NEW** | Today `allow_live` is a manual switch. Nothing reads the calibration report and *refuses* live until positive net EV + controlled drawdown over min sample. Acceptance 11.7. |
| Kill switch (cancel all + block new, mid-cycle) | **Reuse** | `execution_adapter.cancel_all`, `_gate_state()` STOP files, `cli kill --flatten`. |
| Reconciliation gate (halt on drift) | **Reuse** | `pqb/reconcile.py` (`should_halt`, `halt_on_mismatch`), wired into the run loop pre-decision. |
| Pre-trade risk limits (max single order, max open exposure, max daily loss) → breach halts | **Extend** | `max_open_positions` + `max_position_fraction` exist. **Gap: `max_single_order_usdc`, `max_open_exposure_usdc`, `max_daily_loss_usdc` breach-halt** are not distinct config limits — add. |
| Idempotent orders via client-generated IDs | **BUILD-NEW** | No client-order-id / idempotency key found in `execution_adapter`; upstream `trading.py` doesn't set one either. Add a client-generated id + in-flight de-dup. |
| Append-only decision journal (inputs, rationale, later outcome) | **Reuse** | `pqb/journal.py` records decisions (incl. no-op) + outcomes. |
| No access circumvention (no IP/geo/VPN spoofing) | **Reuse** | None present; nothing to add. Stated as a non-goal. |

## §4 — Polymarket market-mechanics interpretation layer

| Capability | Status | Where / gap |
|---|---|---|
| Price = implied probability, payoff model | **Reuse** | Encoded throughout `decision/expected_value.py` (EV = P − C on $1 payoff). |
| Bid/ask + **full book depth**, achievable fill | **Extend** | `decision/expected_value.executable_price` walks the book; upstream `Book` (`price_service.py`) gives full depth **while the WS stream is live** (REST fallback collapses to top-of-book). Gap: depth reliability off-stream — document + prefer stream. |
| Liquidity as a sizing variable | **Reuse** | Size evaluated against book via `executable_price` + `adapters/sizing.py`. |
| Reconstruct **true positions** per wallet/market (net shares, avg entry, realized/unrealized, adds/exits) | **BUILD-NEW** (CLOB part **Extend**) | `analytics/lifecycle.py` reconstructs from **CLOB trades + resolutions** (this session). **Missing the CTF half** — see §6. Upstream has only per-copied-order bookkeeping, no netting engine. |
| Direction (buy YES ≠ sell YES) → net position | **Reuse** | `lifecycle.py` / `reverse.py` net by token, not trade count. |
| Time-to-resolution, probability history | **Extend** | End date via Gamma (`end_ts`); **price/probability history: gap** — no `/prices-history` in either repo. Add a CLOB price-history read. |
| Resolution rules ingest + ambiguous-resolution flag | **Extend** | Gamma gives `outcomePrices`/decisive-resolution; **explicit ambiguous-resolution flag: gap** — add. |
| **Fees — per-market, maker vs taker** | **BUILD-NEW** | Both repos assume flat/zero fees (`fee_per_trade_usdc` here; nothing upstream). Add per-market fee retrieval + maker/taker in EV. |
| Use Gamma / Data / CLOB / WS together | **Extend** | Gamma + Data + CLOB REST reused from upstream. WS market/user channels exist upstream but this engine currently **polls**; streaming integration is a gap (lower priority for paper). |

## §5 — Three-layer decision architecture

| Layer | Status | Where |
|---|---|---|
| **A — Wallet quality** (shrinkage, recency, specialization, consistency) → prior, not per-trade P | **Reuse** | `analytics/ranking.py` + `features.py` (shrinkage K=25, staleness decay, sample floor). **Market specialization** partially via `segments.py`; per-category *quality scoring* is an **extend**. |
| **B — Trade quality** (this position: entry, direction, movement, liquidity, spread, TTR, adds) | **Extend** | Inputs exist across `lifecycle.py` + book data; assembled into a `TradeQuality` signal — audit coverage vs the spec's field list. |
| **C — Execution/exit EV** (achievable price after spread/slippage/fees) | **Extend** | `decision/expected_value.py`; correct once the **per-market fee** gap is closed. |
| Calibrated **P** combines all inputs; wallet score is an input, never P | **Reuse** | `decision/probability.py` (market price prior + evidence in log-odds, sample-weighted, capped). This is the core fix for `score→BUY`. |
| Net EV gate (min edge, sample-backed) | **Reuse** | `min_expected_value`, `min_confidence` in config + `ev_engine`. |
| Wallet **consensus** with correlated-wallet de-dup | **Reuse** | `analytics/correlation.py` (`independent_subset`, union-find clustering). |
| Position-size normalized to wallet's own typical size | **Extend** | `features.py` has per-wallet size stats; the **normalized-size signal** into P is an audit/extend. |
| **Our-account** proportional normalization (never raw $) | **Reuse** | `adapters/sizing.py` sizes off our portfolio fraction, not their absolute $. |

## §6 — Wallet behaviour reconstruction (done honestly) — **the largest gap**

| Capability | Status | Where / gap |
|---|---|---|
| Lifecycle (why enter, how add, when/why flip to opposite, inventory) | **Reuse** | `analytics/lifecycle.py` (this session): entry, adds (avg up/down), the flip, defend-vs-lock, "still-at-risk" cash flow. |
| Per-wallet, per-market decomposition; focus top ~25 | **Reuse** | `segments.py` + `cli strategies` / `lifecycle`. |
| **True P&L from CLOB + CTF split/merge/redeem + ERC-1155 transfers** | **BUILD-NEW** | **Neither repo has any on-chain layer.** Needs a web3 client, a Polygon RPC endpoint, ConditionalTokens (+ NegRisk) ABIs/addresses, event reads for `PositionSplit`/`PositionsMerge`/`PayoutRedemption` and ERC-1155 `TransferSingle`/`Batch`, folded into the netting engine. **This is what makes the "100% win rate" shrink** (acceptance 11.3) and is the biggest single build. **External dependency: an RPC URL.** |
| Output = characterized behaviour + measured historical EV, confidence-flagged by sample | **Extend** | `lifecycle.profile` + `playbook`; add explicit confidence flag tied to sample size (partly present). |

## §7 — Opportunity ranking & position management

| Capability | Status | Where |
|---|---|---|
| Score every candidate on net EV; compare vs open positions | **Reuse** | `decision/portfolio.build_plan`. |
| At limit, replace lowest-EV only if new EV materially higher (margin) | **Reuse** | `portfolio.py` `replace_margin`; no "DO_NOTHING because full" stall. |
| Compounding/baseline sizing subordinate to EV + risk limits; persist across restarts | **Reuse** | `doubling.py` + journal-persisted progression; EV/risk win over sizing. |

## §8 — Learning & calibration loop

| Capability | Status | Where / gap |
|---|---|---|
| On resolution, compare predicted P vs outcome | **Reuse** | `decision/calibration.py` + `analytics/feedback.py`. |
| Track calibration **by wallet / category / prob-bucket / TTR / size / consensus** | **Extend** | Prob-bucket + Brier-vs-market exist; **breakdowns by category / TTR / consensus: gap** — add dimensions. |
| Paper-mode report drives the live gate | **Extend** | `scripts/calibration_report.py` exists; **wire it to the code-enforced gate** (§3 build-new). |
| Overfitting guards (hold-out, out-of-sample, honest "no edge") | **Extend** | Records rejected candidates; add explicit hold-out / OOS split + a plain "no edge" verdict. |

---

## Task plan (ordered by dependency & risk; each maps to an acceptance check)

**Phase 0 — cheap safety closures (no external deps).**
1. Idempotent client-generated order IDs + in-flight de-dup — `execution_adapter`. (11.8)
2. Explicit risk limits `max_single_order_usdc`, `max_open_exposure_usdc`, `max_daily_loss_usdc` with breach→halt — `config.py` + runner gate. (§3, 11.8)
3. Interactive confirmation on live start — `cli run` / `runner`. (§3)

**Phase 1 — EV correctness: per-market fees.** Retrieve maker/taker fee params (Gamma/CLOB), thread through `expected_value.py`; taker fees dominate near mid-range. (§4, §5-C, 11.4)

**Phase 2 — code-enforced live calibration gate.** A gate function reads the paper calibration report and refuses live unless net EV > 0, drawdown < limit, sample ≥ min. Blocks `mode.live` regardless of `allow_live`. (§3, 11.7)

**Phase 3 — CTF / on-chain reconstruction (largest; needs RPC).**
  a. web3 client + `polygon_rpc_url` config + ConditionalTokens/NegRisk addresses & ABIs.
  b. Event reads: `PositionSplit`, `PositionsMerge`, `PayoutRedemption`.
  c. ERC-1155 `TransferSingle`/`TransferBatch` reads.
  d. Fold CLOB + CTF + transfers into one netting/cost-basis engine → true per-wallet P&L.
  e. **Demonstrate the delta** (CTF-inclusive vs CLOB-only) on the provided wallet
     `0x2005…875ea`. (§6, **11.3**)

**Phase 4 — calibration breakdowns + overfitting guards.** Add category / TTR / consensus dimensions; hold-out + OOS + honest "no edge" verdict. (§8, 11.5)

**Phase 5 — streaming & price-history (lower priority; polling suffices for paper).** Integrate upstream WS market/user channels into the engine; add CLOB `/prices-history`. (§4)

### Sequencing notes
- Phases 0–2 are independent and can land first; they close acceptance 11.4/11.7/11.8 and carry no external dependency.
- **Phase 3 is gated on one decision: which Polygon RPC endpoint** (public RPC vs a keyed provider). It is the critical path for acceptance 11.3 and the honest "not lossless" demonstration.
- Phases 4–5 refine; not blocking for a first paper calibration gate.

### Open decisions to confirm before coding
1. **Polygon RPC** for Phase 3 — public endpoint, or a provider key you supply? (Determines whether 11.3 can run at full history depth.)
2. **Fee source** — pull per-market fees live from the API, or configure a maker/taker schedule until the live values are confirmed?
3. **Scope of first delivery** — land Phases 0–2 (safety + fee-correct EV + code-enforced gate) as a reviewable increment, then do Phase 3 (CTF) as its own increment? Recommended, since Phase 3 is large and has the external dependency.

---

## Implementation status (confirmed choices: all phases in one pass; fees live from API; RPC decided at Phase 3)

| # | Acceptance criterion | Status |
|---|---|---|
| 11.1 | Reuse Map + plan produced and confirmed before coding | ✅ this document |
| 11.2 | Interpretation layer exposes depth, achievable fill, **fees**, TTR, resolution | ✅ fees now per-market (`taker_fee_bps`); depth/fill/TTR already present |
| 11.3 | Positions reconstructed from CLOB **and CTF** split/merge/redeem + transfers; per-wallet P&L differs; demonstrate on the provided wallet | ⏳ **code complete + unit-tested** (`pqb/chain/`, `cli onchain`); **live demonstration on `0x2005…875ea` pending the Polygon RPC choice** — the one item that cannot run until the RPC endpoint is set |
| 11.4 | Engine outputs a calibrated per-trade P and a net-EV figure; not wallet score as probability | ✅ already true; fee model now makes net EV fee-correct |
| 11.5 | Sample-size-aware scoring ranks 39%/187 over 100%/11 | ✅ pre-existing shrinkage; calibration now also breaks down by category/TTR/consensus + OOS hold-out |
| 11.6 | Continuous re-ranking replaces a lower-EV open position | ✅ pre-existing (`portfolio.build_plan`, `replace_margin`) |
| 11.7 | Paper-mode calibration report; live **code-blocked** until it passes | ✅ `pqb/decision/live_gate.py`, enforced in `Runner.start`, inspectable via `cli gate` |
| 11.8 | Kill switch, reconciliation-halt, risk-limit blocks, secret redaction verified | ✅ kill/reconcile pre-existing; **added** idempotent order IDs, single-order cap, exposure + daily-loss entry-blocks, interactive live confirm |

**Tests:** 362 pass (45 added this session — `test_risk_controls`, `test_fees`, `test_live_gate`, `test_ctf`, `test_calibration_breakdown`, `test_prices_history`, `test_lifecycle`).

**§4 note carried out:** WebSocket streaming was already integrated via the upstream `PriceService`; the genuine gap was CLOB `/prices-history`, now added (`cli history`).

**The one thing that is not "done" and is not claimed as done:** the live 11.3 run. Its code path is written and its decoder is proven against synthetic on-chain logs, but the on-chain read needs a Polygon RPC endpoint, which was deferred to Phase 3. `cli onchain <wallet>` shows the CLOB-only P&L today and the full CLOB-vs-CTF delta the moment `PQB_POLYGON_RPC_URL` is set.
