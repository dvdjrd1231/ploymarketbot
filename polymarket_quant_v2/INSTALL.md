# Install and run — for David

Everything here is offline. No pip install, no network, no API key, no build
step.

---

## The one-step version

Unzip so `polymarket_quant_v2/` sits beside `Polymarket-Bot-DAVID/` and
`Polymarket-Bot-DATA/`, then **double-click `INSTALL.bat`**.

It does all of it, in order, and stops with a clear message if anything is
wrong:

```
[ 1/13 ] Python 3.13.13 OK
[ 2/13 ] Running the test suite (no database needed)...
         143 passed, 3 skipped
[ 3/13 ] Checking access to your data...
  [PASS] substrate reachable                       116,923 copyable trades
  [PASS] every global gate carries evidence        []
  [PASS] Strategy A gates cannot block Strategy B  ...
  [PASS] funnel reconciles
[ 4/13 ] audit      - where the existing engine's opportunities go
[ 5/13 ] reconcile  - reconciliation exit safety, before/after
[ 6/13 ] inventory  - how much evidence actually exists
[ 7/13 ] features   - which features inform, which are inert
[ 8/13 ] rn1        - reconstructing the reference wallet
[ 9/13 ] discover   - discovery + validation (the slow step)
[10/13 ] winners    - what separates big winners from big losers
[11/13 ] exits      - hold to settlement, or exit early
[12/13 ] expansion  - Win Expansion ladder and staking modes
[13/13 ] shadow     - full pipeline replayed over history

  ... DASHBOARD ...
  ... DIAGNOSTIC - the 22 questions ...
```

Takes about five minutes. Full transcript in `var\install-run.log`, JSON in
`var\reports\`. Safe to re-run, safe to Ctrl-C.

If your data is not in the default place, set the path once and re-run:

```cmd
set PQV2_DATA_DB=D:\path\to\Polymarket-Bot-DATA\state\intel.sqlite3
```

For scripted / unattended runs, `set PQV2_NO_PAUSE=1` skips the final prompt.

**The rest of this document is the manual equivalent**, for when you want to
run one piece at a time.

---

## 1. Requirements

- **Python 3.11 or newer** (`python --version` to check)
- Nothing else. The package uses the standard library only.

Optional, and **not needed**: Rust + maturin, only if you ever build the
acceleration extension. `docs/PERFORMANCE.md` explains why you should not yet.

---

## 2. Unzip

```
Polymarket-Quant-Engine-V2-for-David.zip
└── polymarket_quant_v2/
```

Put `polymarket_quant_v2/` **beside** your existing `Polymarket-Bot-DAVID/`
folder, so the layout is:

```
<your repo root>/
├── Polymarket-Bot-DAVID/          <- your existing system, untouched
├── Polymarket-Bot-DATA/           <- your data
└── polymarket_quant_v2/           <- NEW, from the zip
```

That placement matters: V2 finds the database by walking one level up to
`Polymarket-Bot-DATA/state/intel.sqlite3`. If you put it elsewhere, set the
path explicitly in step 4.

---

## 3. Verify the package before pointing it at your data

```bash
cd polymarket_quant_v2
python -m pytest tests/ -q
```

Expected: **`110 passed, 3 skipped`** in about 5 seconds.

The 3 skips are Rust equivalence tests and are expected — the extension is not
built. These tests need no database; they run on synthetic fixtures.

---

## 4. Point it at your data

If you used the layout in step 2, it is already correct. Otherwise:

**Windows (cmd):**
```cmd
set PQV2_DATA_DB=D:\path\to\Polymarket-Bot-DATA\state\intel.sqlite3
set PQV2_WORK_DIR=D:\path\to\polymarket_quant_v2\var
```

**Windows (PowerShell):**
```powershell
$env:PQV2_DATA_DB="D:\path\to\Polymarket-Bot-DATA\state\intel.sqlite3"
$env:PQV2_WORK_DIR="D:\path\to\polymarket_quant_v2\var"
```

**macOS / Linux:**
```bash
export PQV2_DATA_DB=/path/to/Polymarket-Bot-DATA/state/intel.sqlite3
export PQV2_WORK_DIR=/path/to/polymarket_quant_v2/var
```

Or pass them per command: `python -m pqv2 --data-db <path> --work-dir <path> ...`

Confirm it can see the data:

```bash
python -m pqv2 selftest
```

Expected — four `[PASS]` lines, including the copyable-trade count:

```
SELFTEST
  [PASS] substrate reachable                       116,923 copyable trades
  [PASS] every global gate carries evidence        []
  [PASS] Strategy A gates cannot block Strategy B  gates.assert_may_block raised as required
  [PASS] funnel reconciles
```

---

## 5. Is my original system safe?

Yes, and you can prove it rather than trust it:

```bash
git status Polymarket-Bot-DAVID/
```

Empty output = nothing changed. V2 opens every original database with
`mode=ro` and `PRAGMA query_only=ON`, and writes only under `var/`. The test
`test_v2_never_writes_to_the_v1_installation` inspects every database call in
the package by AST and fails the build if one opens a file for writing outside
V2's own stores.

**You can also delete `polymarket_quant_v2/` at any time with no effect on your
existing system.**

---

## 6. The tour — run these in order

Each takes seconds except `discover`.

```bash
python -m pqv2 audit         # where your engine's 40,820 opportunities went
python -m pqv2 inventory     # how much evidence actually exists
python -m pqv2 features      # which features are inert, and what that costs
python -m pqv2 rn1           # reconstruct the reference wallet
python -m pqv2 discover -v   # discovery + validation   (~90 s, 40 wallets)
python -m pqv2 winners       # what separates big winners from big losers
python -m pqv2 exits         # hold to settlement, or exit early?
python -m pqv2 expansion     # the Win Expansion ladder + staking modes
python -m pqv2 shadow        # full pipeline replayed over history
python -m pqv2 dashboard     # everything, one screen
python -m pqv2 diagnose      # the 22 mandatory questions, answered
```

**Start with `audit`.** It is the one that explains the zero-trade problem, and
it will also flag the two already-validated strategies that nothing in your bot
reads.

`discover` and `shadow` write into `var/`; the rest read what they wrote. Run
`discover` before `dashboard`, `leaderboard` or `diagnose`, or those will show
an empty pass.

---

## 7. Useful options

```bash
# Use YOUR RN1 address instead of letting the engine pick one from data.
# Strongly preferred - an externally-named wallet costs no statistical power.
python -m pqv2 rn1      --wallet 0xYOURWALLET
python -m pqv2 discover --wallet 0xYOURWALLET -v

# Sweep more wallets (211 are eligible; default 40). ~2s per wallet.
python -m pqv2 discover --max-wallets 120 -v

# Change starting capital and the out-of-sample fraction
python -m pqv2 --capital 25000 --oos 0.25 discover

# Only show what survived
python -m pqv2 leaderboard --status VALIDATED

# Keep runs separate (each --work-dir is an independent research database)
python -m pqv2 --work-dir var_run2 discover -v
```

---

## 8. Where the output goes

```
polymarket_quant_v2/var/
├── research/
│   ├── registry.sqlite3     every strategy ever tested + the denominator
│   └── ledger.sqlite3       every signal, its terminal state and gate
└── reports/
    ├── last_pass.json       the discovery pass
    ├── strategy_a_audit.json
    ├── feature_audit.json
    ├── winners.json  exits.json  expansion.json  shadow.json
    └── diagnostic.json      the 22 answers
```

All JSON, all safe to delete — re-running regenerates them.

---

## 9. Reading order for the documents

| read | for |
|---|---|
| `HANDOVER.md` | **start here** — the whole story in two pages |
| `docs/FINDINGS.md` | what the first pass found, with reproducing commands |
| `docs/PRIOR-WORK.md` | the two validated strategies nothing reads — and why not to connect them yet |
| `docs/MAPPING.md` | your existing system mapped, with the SQL |
| `docs/LIMITS.md` | what this data cannot answer, and what would fix each |
| `docs/REQUIREMENTS-AUDIT.md` | requirement-by-requirement status, including gaps |
| `docs/ARCHITECTURE.md` | how it is built |
| `docs/PERFORMANCE.md` | the profile, the 2.8× fix, when Rust becomes worth it |

---

## 10. Troubleshooting

**`No module named pqv2`** — you are in the wrong directory. `cd` into
`polymarket_quant_v2/` (the folder containing `pqv2/`) first.

**`selftest` shows 0 copyable trades** — `PQV2_DATA_DB` is not pointing at
`intel.sqlite3`. Check the path exists and the file is ~2.6 GB.

**`Device or resource busy` / `database is locked`** — something else has the
file open, or a previous run is still going. Use a different `--work-dir`.

**`discover` says 0 validated** — that is a legitimate result, not a failure.
Read the status histogram: it tells you *which bar* candidates stopped at, and
`docs/FINDINGS.md` explains what each one means.

**Tests fail** — send the output. They need no database, so a failure is a real
portability problem, not a configuration one.

---

## 11. What NOT to do

- **Do not connect the two `walletlab` strategies to live trading.** Read
  `docs/PRIOR-WORK.md` first — V2 measures those same wallets as strongly
  negative out-of-sample and the disagreement is unresolved.
- **Do not treat `VALIDATED` as "profitable".** It means *survived historical
  out-of-sample validation* and authorises **paper trading only**.
- **Do not loosen the entry filters in your existing engine** to increase trade
  count. The audit shows they were never the constraint.
- **Do not build the Rust extension yet.** `python -m pqv2 accel` tells you
  when it becomes worth it.
