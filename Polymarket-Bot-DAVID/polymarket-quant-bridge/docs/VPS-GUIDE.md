# Running the Polymarket Quant Bridge on a VPS

Everything needed to take this from a fresh server to a supervised live trade,
in the order it should be done.

Read [§7 Before you go live](#7-before-you-go-live) before touching the two
flags that arm real trading. Nothing before that point can place an order.

---

## 1. What the server needs

| | Minimum | Recommended | Why |
|---|---|---|---|
| **OS** | Debian 12 / Ubuntu 22.04 | Debian 13 / Ubuntu 24.04 | The installer targets apt + systemd. |
| **CPU** | 2 vCPU | 4 vCPU | Feature engineering is per-column pandas work; discovery is CPU-bound and runs for minutes. |
| **RAM** | 2 GB | 4 GB | The trading loop sits around 250–400 MB. Strategy discovery is the peak and is capped at 1.5 GB by its unit. |
| **Disk** | 20 GB SSD | 40 GB SSD | See the growth figures in §8. |
| **Network** | any | — | **Outbound only.** No inbound port is needed; do not open one. |

Measured on the reference machine, so you can size against real numbers rather
than guesses:

- Live feature engineering: **~66 ms per token per cycle**, ~3.2 s for a
  48-token universe. (Before the column-reduction fix this was 856 ms/token and
  **41 s per cycle** — if you are running an older checkout, take that
  seriously.)
- A full research run over 4 token series: **~4 minutes**, peak well under
  1.5 GB.
- Steady-state trading cycle: **0.5–7 s** depending on how many markets are in
  the universe.

**A 1 GB VPS is not enough.** Discovery will be OOM-killed, and because it runs
from a timer the failure is quiet — you get no new strategies and no obvious
error. If 1 GB is all you have, run discovery elsewhere and copy
`state/strategies.json` in.

### Location

Polymarket's APIs are global, but latency affects fill quality on
Fill-And-Kill orders. Anywhere in the US or Western Europe is fine. Avoid
regions that geoblock Polymarket.

---

## 1.5 Two ways to install — pick one

| | **Docker** (§2b) | **Native systemd** (§2–§5) |
|---|---|---|
| Three-repo layout | Handled by the build; cannot be got wrong | You must place them correctly — the usual failure |
| Dependencies | Baked into the image | apt + one venv, built by the installer |
| Upgrade | Rebuild, `up -d` | rsync, re-run `install.sh` |
| Scheduling | Host cron calls `docker compose run` | systemd timers, included |
| Best when | You want it running today, or you run other containers | You want journald, timers and systemd hardening |

Both use the same code, the same config file and the same state directory, and
you can move between them by copying `state/` and `config/`.

> **Status of the Docker path.** The image was authored but **not built or run
> here** — this machine has no Docker daemon. The compose file is validated,
> the scripts are syntax-checked, and `build.sh` ends with a verification step
> that fails the build if the image cannot see all three projects. Treat the
> first `build.sh` as the real test. The native path in §3 *has* been exercised
> end to end.

---

## 2b. Docker

### Build

Run this on a machine that has all three repositories — your workstation is
fine, and you can then push the image or save it to a tarball.

```bash
cd polymarket-quant-bridge
bash deploy/docker/build.sh
```

Docker takes exactly one build context, but this system spans three
repositories — so the script stages them into a temporary directory and builds
from that, rather than dragging every unrelated sibling project in from a
shared parent. It auto-detects `ploymarketbot` and the **inner**
`qc_lean_bridge` folder beside the project; override with `UPSTREAM_SRC=` and
`BRIDGE_SRC=` if they live elsewhere.

The last thing it does is start the image and confirm both siblings resolve, so
a build that would fail at runtime fails at build time instead.

To move it to the server without a registry:

```bash
docker save pqb:latest | gzip | ssh root@YOUR_VPS 'gunzip | docker load'
```

### Configure

```bash
cp config/config.example.yaml config/config.yaml   # dry-run by default
cp deploy/docker/pqb.env.example .env              # leave the key BLANK
chmod 600 .env
mkdir -p state backups && sudo chown -R 10001:10001 state
```

The last line matters: the container runs as uid 10001, and a bind mount
inherits the host's ownership. Without it the first cycle fails on a confusing
SQLite permission error — the entrypoint checks for this and tells you, but it
is easier to just set it.

### Run

```bash
docker compose up -d
docker compose logs -f
```

Everything else is the same CLI, in the same image, against the same state:

```bash
docker compose run --rm cli check
docker compose run --rm cli status
docker compose run --rm cli wallets
docker compose run --rm cli anomalies
docker compose run --rm cli report
docker compose run --rm cli kill          # and: resume
docker compose run --rm research          # strategy discovery
docker compose run --rm backup            # writes to ./backups
```

### Schedule the recurring jobs

Compose has no scheduler, so this is the one thing the systemd path gives you
for free. Add to the host's crontab (`sudo crontab -e`):

```cron
# Nightly backup at 03:30 UTC, then strategy discovery at 04:10 UTC.
30 3 * * * cd /opt/polymarket-quant-bridge && /usr/bin/docker compose run --rm backup   >> /var/log/pqb-backup.log 2>&1
10 4 * * * cd /opt/polymarket-quant-bridge && /usr/bin/docker compose run --rm research >> /var/log/pqb-research.log 2>&1
```

### Notes specific to containers

- **The clock is the host's.** Keep the host NTP-synced; a drifting clock
  silently mis-ages every quote and wallet observation.
- **`stop_signal: SIGINT`** is set deliberately. The runner installs its
  graceful-shutdown handler on SIGINT; Docker's default SIGTERM would be a hard
  stop mid-cycle.
- **The healthcheck treats "degraded" as alive.** A bridge with no strategies
  yet is still doing useful work, and restarting it would only lose progress.
  Only a genuine failure (loop stopped, halt engaged, no wallets observed) is
  reported unhealthy.
- **`state/` is a bind mount, not a named volume** — the journal and the intel
  store are the two things worth backing up, and a directory you can see and
  copy is much harder to lose than a volume somebody prunes.
- The image contains **no secrets and no config**. Both arrive at runtime, so
  the same image is safe to keep in a registry and move between machines.

Then continue at [§6 Let it learn](#6-let-it-learn-then-give-it-a-brain) — the
timeline is identical.

---

## 2. The three repositories

This is the step people get wrong, so it comes before the installer.

The project depends on **two sibling projects**, reached through path shims —
not pip packages:

```
/opt/
├── polymarket-quant-bridge/   this project
├── ploymarketbot/             Polymarket clients        (pqb/upstream.py)
└── qc_lean_bridge/            the Quant Bridge / brain  (pqb/quant.py)
```

Note the spelling of `ploymarketbot` — it is spelled that way in the original
repository and the shim expects it.

`qc_lean_bridge` is the **inner folder** of "Advanced Strategy Development for
QuantConnect LEAN", not the outer one.

From your workstation:

```bash
# 1. This project
rsync -a --exclude .venv --exclude state --exclude __pycache__ \
      polymarket-quant-bridge/ root@YOUR_VPS:/opt/polymarket-quant-bridge/

# 2. The Polymarket clients
rsync -a --exclude .venv --exclude __pycache__ \
      ploymarketbot/ root@YOUR_VPS:/opt/ploymarketbot/

# 3. The brain — note: the INNER qc_lean_bridge folder
rsync -a --exclude .venv --exclude __pycache__ --exclude dist --exclude build \
      "Advanced Strategy Development for QuantConnect LEAN/qc_lean_bridge/" \
      root@YOUR_VPS:/opt/qc_lean_bridge/
```

If you keep them somewhere else, set `PQB_POLYMARKETBOT_PATH` and
`PQB_QUANT_BRIDGE_PATH` in `/etc/pqb/pqb.env` — the installer writes both.

---

## 3. Install

```bash
ssh root@YOUR_VPS
cd /opt/polymarket-quant-bridge
sudo bash deploy/install.sh
```

The script is idempotent — re-run it after copying a missing repo, or to
upgrade. It never overwrites `config/config.yaml`, `/etc/pqb/pqb.env`, or
`state/`.

What it does:

1. Installs `python3-venv`, `sqlite3`, `git`, and a time-sync daemon.
   **Clock sync matters**: the bridge compares exchange timestamps with its own
   clock, and drift silently mis-ages every quote and wallet observation.
2. Creates the unprivileged `pqb` service user.
3. Builds **one** virtualenv at `/opt/polymarket-quant-bridge/.venv` holding all
   three projects' dependencies. This is deliberate — `pqb.cli run` needs the
   Polymarket clients *and* the bridge's feature engineering in one process, so
   two environments cannot work.
4. Writes `/etc/pqb/pqb.env`, chmod 600, owned by `pqb`.
5. Installs the systemd units and enables the research and backup timers.

It does **not** start trading. The service is left stopped and the config is in
dry-run.

> **PyQt6 is deliberately not installed.** It is in the Quant Bridge's own
> `requirements.txt` for its desktop GUI, costs ~85 MB and needs X libraries.
> Nothing on the paths used here imports it — verified.

---

## 4. Verify before starting

```bash
sudo -u pqb /opt/polymarket-quant-bridge/.venv/bin/python -m pqb.cli \
     --config /opt/polymarket-quant-bridge/config/config.yaml check
```

Every line should resolve. The ones that matter:

```
upstream    : /opt/ploymarketbot  (ploymarketbot)
quant bridge: /opt/qc_lean_bridge
intel       : broad ingestion on, cohort=25, lookback=30d, global_sweep=True
anomalies   : on, 6 detectors
engine      : pqb.bridge.lean_engine:LeanDecisionEngine
engine load : ok
mode        : dry-run
```

`quant bridge: NOT AVAILABLE` means the brain is missing — the system will still
run, but on baseline scoring, and `pqb.cli research` cannot run at all. Fix it
before continuing.

To save typing, add a shell alias:

```bash
echo "alias pqb='sudo -u pqb /opt/polymarket-quant-bridge/.venv/bin/python -m pqb.cli --config /opt/polymarket-quant-bridge/config/config.yaml'" \
  >> ~/.bashrc && source ~/.bashrc
pqb check
```

The rest of this guide uses that alias.

---

## 5. Start it — in dry-run

```bash
sudo systemctl start pqb
sudo systemctl enable pqb          # survive a reboot
journalctl -u pqb -f
```

Within a minute or two you should see, per cycle:

```
event=universe.refreshed markets=25 tokens=50
event=intel.ingest observed=2998 new=27 wallets=1466
event=intel.anomalies new=8 active=8 convergence=5 size_spike=2 behaviour_drift=1
event=intel.ranked observed=1936 ranked=129 scored=1312
event=cycle markets=25 positions=0 wallets=1936 ranked=129 anomalies=8 decisions=1 ...
```

That is the whole pipeline working: broad ingestion → ranking → anomaly
detection → decision.

Check health at any point:

```bash
bash /opt/polymarket-quant-bridge/scripts/healthcheck.sh
```

It reports HEALTHY / DEGRADED / UNHEALTHY and exits 0 / 1 / 2, so it drops
straight into cron or an uptime monitor.

---

## 6. Let it learn, then give it a brain

**This is the part that takes time, and it cannot be skipped or shortened.**

The feature series discovery runs on is captured live and **cannot be
backfilled** — an order book that existed for four seconds last Tuesday is
gone. So the bridge has to run before it can be researched.

| Wait | What becomes possible |
|---|---|
| ~1 hour | Live feature engineering activates (needs 60 rows per token). |
| **~4 hours** | Enough captured rows (200/token) for the first real `research` run. |
| ~1–3 days | Markets settle, so wallet ranking is scored against **settled outcomes** rather than live marks. This is when the ranking becomes properly meaningful. |
| ~1 week | The journal has enough closed positions for the feedback loop to start tilting decisions (needs 20 closed, 5 per group). |

Watch it accumulate:

```bash
pqb status        # includes wallets observed/ranked, research rows captured
pqb wallets       # the ranking, as the system derived it
pqb anomalies     # what the detectors found, with the numbers behind each
```

Once `pqb status` shows a few thousand research rows, run discovery:

```bash
sudo systemctl start pqb-research      # or: pqb research
journalctl -u pqb-research -f
```

It exports one CSV per token, runs each through the Quant Bridge's discovery →
walk-forward → Monte Carlo → ranking, and keeps only rules accepted on **two or
more independent token series**. The result lands in `state/strategies.json`,
and the running service picks it up within ten minutes — no restart.

After that it runs nightly at 04:10 UTC on its own:

```bash
systemctl list-timers 'pqb-*'
```

**"0 rules kept" is a result, not a failure.** It means nothing held up across
more than one series. Let it collect more history and try again — that is the
cross-token check doing its job rather than handing you a curve fit.

---

## 7. Before you go live

Everything to this point is simulated. Live trading needs **two** config flags
plus a key, so one careless edit cannot arm it.

### 7.1 Fund the wallet — on Polygon

Polymarket settles in **USDC on Polygon (chain 137)**. USDC on Ethereum
mainnet must be **bridged to Polygon first**. An unbridged wallet reads as a
zero balance and every order fails, with nothing in the error naming the cause.

### 7.2 Install the key

```bash
sudo nano /etc/pqb/pqb.env
```

```ini
PQB_PRIVATE_KEY=0xyourprivatekey
PQB_FUNDER_ADDRESS=0xyourwalletaddress
```

```bash
sudo chown pqb:pqb /etc/pqb/pqb.env && sudo chmod 600 /etc/pqb/pqb.env
```

**Key custody.** The key is read on this machine and nowhere else. It is never
transmitted, logged, committed, or returned by any API path, and it is dropped
from memory once the exchange client connects. Whoever controls this host and
its credentials controls the wallet — so **the wallet owner should own the
host**, not a developer. Use a wallet funded only with what you are prepared to
lose.

### 7.3 Arm it, with the smallest possible size

```bash
sudo nano /opt/polymarket-quant-bridge/config/config.yaml
```

```yaml
mode:
  dry_run: false
  allow_live: true

engine:
  portfolio:
    min_order_usdc: 1.0          # the exchange minimum
    max_position_fraction: 0.05  # small, for the first live session
    max_open_positions: 2
```

```bash
pqb check                        # now prints the LIVE TRADING banner
sudo systemctl restart pqb
```

### 7.4 The first live order — supervised

Have this ready in a second terminal **before** you restart:

```bash
pqb kill              # stop new orders immediately
pqb kill --flatten    # ...and close every open position
pqb resume            # clear it
```

Both take effect within one cycle, need no redeploy, work from any shell, and
survive your SSH session dropping.

Watch one order place, fill, and reconcile:

```bash
journalctl -u pqb -f | grep -E "execution|position|reconcile"
```

Then stop and check the books against the Polymarket UI before letting it run
unattended. Do not skip this: everything on the live path is written and
unit-tested, but **"tested" and "traded" are different words** — no order from
this system has ever reached the real exchange.

---

## 8. Operating it

### Daily

```bash
pqb status                     # doubling state, positions, analytics counters
pqb report                     # what has actually worked
bash scripts/healthcheck.sh
```

### The two stop files

| File | Set by | Effect |
|---|---|---|
| `state/KILL` | you (`pqb kill`) | Halts new orders. `--flatten` also closes out. |
| `state/HALT` | reconciliation, automatically | Halts trading because the exchange and the bridge disagree about what is held. **Never flattens** — closing out is still trading, and a halt means we do not know what we hold. |

A `HALT` persists across restarts on purpose: a divergence nobody has looked at
must not be cleared by bouncing the process.

```bash
pqb resume            # prints what diverged, refuses to clear a HALT
pqb resume --force    # clears it — an assertion that you have reviewed it
```

While either is in force the engine keeps evaluating and journalling, so you can
see what it *wanted* to do. Nothing reaches the exchange.

### Disk growth

Roughly, at default settings:

| Store | Growth | Bounded by |
|---|---|---|
| `intel.sqlite3` — observations | ~50–150 MB/month | `intel.retention_days` (90) |
| `intel.sqlite3` — research rows | ~40 MB/month per 50 tokens | 3× the retention window |
| `journal.sqlite3` | ~5–20 MB/month | not pruned; it is the audit trail |
| `state/pqb.log` | capped | rotation, 5 × 2 MB |

Pruning runs hourly inside the service. Hourly per-market rollups are written
before raw trades are deleted, so anomaly baselines outlive the rows they came
from.

### Backups

Nightly at 03:30 UTC to `/var/backups/pqb`, 14 days retained.

```bash
sudo systemctl start pqb-backup     # run one now
ls -la /var/backups/pqb
```

The archive holds the journal, the intel store, `strategies.json` and the
config — **never** the `.env`. Two of those are irreplaceable:

- the **journal** holds the doubling-rule baseline and progression index;
  losing it rewinds the minimum trade size to 0.19 and turns every open
  position into an "unknown position" that halts trading;
- the **intel store** holds the captured feature series, which cannot be
  reconstructed from anything.

**Copy them off the server**, and test a restore before you need one:

```bash
sudo APP_DIR=/tmp/pqb-restore-test bash scripts/restore.sh \
     /var/backups/pqb/pqb-YYYYMMDD-HHMMSS.tar.gz
```

A backup you have never restored is a guess. This one has been round-tripped —
and doing so is how a bug that produced silently-empty archives was found.

### Upgrading

```bash
sudo systemctl stop pqb
# rsync the new code over /opt/polymarket-quant-bridge (state/ and config/ are preserved)
sudo bash deploy/install.sh
pqb check
sudo systemctl start pqb
```

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `quant bridge: NOT AVAILABLE` | `qc_lean_bridge` missing or `pandas`/`numpy` not installed. Copy the **inner** folder; re-run `install.sh`. |
| `upstream : NOT FOUND` | `ploymarketbot` missing. Note the spelling. |
| `markets=0` every cycle | Gamma query failing — usually transient rate-limiting; it degrades gracefully. If persistent, check outbound HTTPS and any geoblock. |
| `ranked=0` with many wallets observed | Normal early on. Ranking needs settled outcomes or live marks; it fills in over the first days as markets resolve. |
| `wallets=0` | Ingestion is not running. Check `intel.enabled: true`. |
| No strategies after `research` | Either too little history (`min_rows`, default 200/token) or nothing survived the cross-token check. Both are reported explicitly in the output. |
| Cycle time creeping up | Check `event=cycle ms=`. If it approaches `cycle_seconds`, reduce `markets.filters.max_markets`. |
| `TRADING HALTED` | Reconciliation found a divergence. Compare `pqb status` against the Polymarket UI, then `pqb resume --force`. |
| Every order rejected, balance reads 0 | USDC is on Ethereum mainnet, not bridged to Polygon. |
| Service restarting repeatedly | `journalctl -u pqb -n 100`. After 5 failures in 5 minutes systemd stops trying and leaves the state for inspection. |
| Discovery killed | OOM against the unit's 1.5 GB cap. Reduce `research.max_tokens`, or use a larger VPS. |

### Useful commands

```bash
journalctl -u pqb -f                     # live
journalctl -u pqb --since "1 hour ago" | grep -E "decision|position"
journalctl -u pqb -p err --since today   # errors only
systemctl list-timers 'pqb-*'
sqlite3 /opt/polymarket-quant-bridge/state/journal.sqlite3 \
  "SELECT action, COUNT(*) FROM decisions GROUP BY action"
```

---

## 10. Security

- **No inbound ports.** The bridge makes only outbound HTTPS/WSS connections.
  If you run a firewall, default-deny inbound except SSH.
- **SSH keys only** — disable password authentication.
- The service runs as an unprivileged user under systemd hardening
  (`ProtectSystem=strict`, `PrivateTmp`, `NoNewPrivileges`), with exactly one
  writable path: `state/`.
- Secrets live only in `/etc/pqb/pqb.env`, chmod 600. They are redacted from
  every log, report and config dump, and never enter journald or a backup.
- Anyone with root on this host can take the wallet. Keep the account list
  short.
