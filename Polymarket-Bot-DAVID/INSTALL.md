# Install guide — David

This bundle contains **source only**. No virtual environment, no databases, no
credentials. Everything below is reproducible from a clean Windows machine.

Expect ~15 minutes.

---

## 0. What you received

```
Polymarket-Bot-DAVID/
  ploymarketbot/            copy-trading bot: FastAPI backend + React dashboard
  polymarket-quant-bridge/  pqb — the research and decision engine (1142 tests)
  qc_lean_bridge/           QuantConnect LEAN bridge
  wallet-strategy-lab/      NEW — wallet strategy discovery (see its README)
  HANDOVER.md               what changed in build 70
  GAPS.md                   what is missing, sorted by priority  <- read this
  INSTALL.md                this file
```

**Deliberately not included**, and why:

| Excluded | Reason |
|---|---|
| `.venv/` (467 MB) | Not relocatable — the bundled one still points at `D:\tasks\olaf_David\...` and fails at test collection. Rebuild it (step 2). |
| `Polymarket-Bot-DATA/` | Runtime state: 2.4 GB of databases, logs and research exports. Sent separately if you want the existing research; otherwise the bot rebuilds it. |
| `__pycache__/`, `.pytest_cache/` | Build artefacts. |
| `config/config.yaml` | Machine-specific. `config.example.yaml` ships; setup creates and configures your copy automatically (step 4). |
| `.env` | Credentials never travel in a zip. Step 5. |

---

## 1. Prerequisites

- **Python 3.11+** — <https://www.python.org/downloads/>. Tick *"Add python.exe
  to PATH"* during install.
- **Node.js 18+** — <https://nodejs.org/> (only for the dashboard).
- Verify:

```powershell
python --version
node --version
```

---

## 2. Unpack and create the environment

```powershell
# Unpack wherever you like; a path with no spaces is safest.
cd D:\polymarket
# (extract Polymarket-Bot-DAVID.zip here, giving D:\polymarket\Polymarket-Bot-DAVID)

cd D:\polymarket\Polymarket-Bot-DAVID\polymarket-quant-bridge
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

If PowerShell blocks the activate script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Optional — the desktop dashboard needs PyQt6:

```powershell
pip install -r requirements-desktop.txt
```

---

## 3. Verify the install before configuring anything

```powershell
python -m pytest tests/ -q
```

Expected: **1142 passed** in roughly two minutes. Five dashboard tests skip if
you did not install PyQt6; that is fine.

If this passes, the engine is sound and anything that goes wrong later is
configuration, not code.

---

## 4. Configuration

If you used `1-INSTALL-FIRST.bat`, this is already done — skip to step 5.

Doing it by hand:

```powershell
copy config\config.example.yaml config\config.yaml
python deploy\windows\_configure_paths.py
```

The second command points the four data paths at your `Polymarket-Bot-DATA`
folder if one was supplied. Without it the config keeps the example's default
(`state/intel.sqlite3`, a local empty folder) and the bot starts against an
**empty database**, silently ignoring the collected history. It does not error;
it just looks like a brand new install.

It only rewrites values still at the template default, so a config you have
edited is never touched, and re-running is safe.

**Where the data folder must sit** — as a sibling of the bundle, not inside it:

```
D:\polymarket\
    Polymarket-Bot-DAVID\        <- the unzipped bundle
    Polymarket-Bot-DATA\         <- alongside
        state\
```

A fresh install with no data folder is fine: it uses a local `state\` folder
and starts collecting from scratch.

**The safety flags ship safe and must both be changed before any real order can
be sent.** Leave them alone until you have paper-traded:

```yaml
mode:
  dry_run: true        # true = simulate only
```

---

## 5. Credentials

Never put a key in `config.yaml` — it reads them from the environment.

```powershell
copy config\.env.example ..\.env
notepad ..\.env
```

Fill in:

```
PQB_PRIVATE_KEY=          # 64 hex chars. Only needed for LIVE trading.
PQB_FUNDER_ADDRESS=       # only for Polymarket email/proxy wallets
PQB_POLYGON_RPC_URL=      # optional; enables on-chain P&L reconstruction
```

Dry-run needs **no key at all**. Do not add one until step 8.

Check it resolves without printing secrets:

```powershell
python -m pqb.cli check
```

### 5a. Connecting a MetaMask wallet

Double-click `polymarket-quant-bridge\deploy\windows\CONNECT-WALLET.bat`, or:

```powershell
python -m pqb.cli wallet-connect
```

It asks for your key at a hidden prompt, works out how your account is set up,
and reports your USDC. It writes only `signature_type` and `funder_address` to
the config — **never the key** — and it does not enable live trading.

Three things worth knowing before you run it:

- **A bot cannot use the MetaMask extension.** There is no browser and nobody to
  click Approve. It needs the private key exported from MetaMask
  (three dots → Account details → Show private key). Same wallet, same address,
  same funds; the extension is simply not in the path.
- **Do not paste your 12-word recovery phrase.** That exposes every account in
  the wallet rather than one. The command refuses it and says so.
- **If it reports `USDC 0.00`, that is usually not a wrong key.** Funding through
  the Polymarket website puts your USDC in a Polymarket *proxy wallet* that your
  MetaMask key controls, not at the MetaMask address. Copy the address shown in
  the Polymarket app and re-run:

  ```powershell
  python -m pqb.cli wallet-connect --funder 0xYourPolymarketAddress
  ```

To check a wallet without changing anything, use `wallet-check`. Add `--offline`
to validate a key and derive its address without contacting Polymarket at all.

The key is never accepted as a command-line argument — argv is visible in the
process list and lands in shell history. Use the prompt, `--key-file`, or
`PQB_PRIVATE_KEY`.

---

## 6. The copy-trading bot and dashboard

```powershell
cd ..\ploymarketbot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

`run.py` builds the React dashboard on first start (this is why Node is
required) and serves it. Open the URL it prints — normally
<http://127.0.0.1:8000>.

Shortcut alternative: `1-INSTALL-FIRST.bat`, then `2-OPEN-DASHBOARD.vbs`.

---

## 7. The new research engine

Standard library only — no install step, and it does not need either venv.

```powershell
cd ..\wallet-strategy-lab
python -m pytest tests\ -q          # 15 passed, ~2 s

$env:WALLETLAB_DATA_DB = "D:\polymarket\Polymarket-Bot-DATA\state\intel.sqlite3"
$env:WALLETLAB_WORK_DIR = "D:\polymarket\Polymarket-Bot-DATA\state\walletlab"

python -m walletlab inventory                 # measure your data
python -m walletlab baselines                 # naive-copy edge per wallet
python -m walletlab discover-strategies       # the full pass, ~40 s for 12 wallets
python -m walletlab leaderboard
python -m walletlab live-signals              # VALIDATED strategies only
```

This one **requires the data folder** — it is a research tool over collected
history, so it has nothing to do without it. Read
`wallet-strategy-lab/README.md` and `docs/AUDIT.md` before acting on output.

---

## 8. Going live — the order that matters

Do not skip steps here. The trading path has **never executed a real order**
(see `GAPS.md` #12: all 40,820 journalled decisions are `DO_NOTHING`). Treat the
first live trade as a first run.

1. Run dry (`dry_run: true`) for several days. Watch the dashboard and
   `pqb lab` / `pqb funnel`.
2. Confirm the journal is recording decisions and the kill switch works: create
   the `KILL` file and confirm the engine stops.
3. Only then add `PQB_PRIVATE_KEY`, fund a small amount, and flip the safety
   flags. Both must change — `dry_run: false` alone is not enough, by design.
4. Start with `max_notional` small. The system has never sized a real position.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'tests.test_engine'` | Running through a copied/stale venv | Delete `.venv`, redo step 2 |
| `ModuleNotFoundError: fastapi` | Wrong venv active for `ploymarketbot` | Activate that project's own venv (step 6) |
| `unable to open database file` | `db:` path in `config.yaml` is wrong or the folder does not exist | Use an absolute path; create the folder |
| Dashboard blank / 404 | Frontend never built | Ensure Node is installed, delete `frontend/dist`, rerun `python run.py` |
| `walletlab` reports 0 wallets | No data folder | Set `WALLETLAB_DATA_DB` to a real `intel.sqlite3` |
| Dashboard looks like a brand-new install; no wallets or history | `config.yaml` points at a local empty `state\` | Check `Polymarket-Bot-DATA` sits *beside* the bundle, then run `python deploy\windows\_configure_paths.py` |
| PowerShell won't run `Activate.ps1` | Execution policy | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` |

---

## 10. Read in this order

1. `GAPS.md` — what is missing and what it costs to fix
2. `wallet-strategy-lab/docs/AUDIT.md` — why the research substrate changed
3. `HANDOVER.md` — build 70 research layer
4. `polymarket-quant-bridge/ARCHITECTURE.md` — what is reused vs new
