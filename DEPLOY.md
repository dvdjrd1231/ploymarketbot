# Deploying to a Linux VPS

Runs the bot 24/7 under `systemd`, bound to `127.0.0.1`. You reach the dashboard
through an SSH tunnel, so **no port is ever opened to the internet**.

Written for Ubuntu 22.04 / 24.04 (Debian 12 is identical). Adapt `apt` for other
distros.

---

## Why an SSH tunnel and not a public port

The API has no login. Every route that arms the engine, sets credentials or
cancels orders is open to whoever can reach the port — that is by design, because
the app was built to listen on localhost only. If you bind it to `0.0.0.0` on a
VPS, anyone who finds the port controls a funded wallet.

The tunnel keeps the bot on localhost and forwards the port over your existing
SSH login, so the only thing exposed is `sshd`.

```
your PC                              VPS
-------                              ---------------------------
browser :8765  ──ssh -L 8765──▶      uvicorn @ 127.0.0.1:8765
                                     ufw: only port 22 open
```

Closing the tunnel does not stop the bot — `systemd` keeps it trading.

---

## 1. Requirements

| | |
|---|---|
| VPS | 1 vCPU / 1 GB RAM is enough to *run*; see step 4 about building |
| OS | Ubuntu 22.04+ |
| Python | 3.10 or newer (22.04 ships 3.10, 24.04 ships 3.12) |
| Node.js | 18+ — **only to build the dashboard**, not to run the bot |
| Network | outbound HTTPS/WSS to Polymarket; inbound only port 22 |

Pick a region close to Polymarket's infrastructure (US East) — copy trading is
latency-sensitive, and a VPS is usually a real improvement over a home
connection here.

## 2. Base system

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git curl build-essential

# Node.js 20 (for the dashboard build)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

python3 --version   # must be >= 3.10
node --version      # must be >= 18
```

Lock the firewall down to SSH:

```bash
sudo ufw allow 22/tcp
sudo ufw --force enable
sudo ufw status          # 8765 must NOT appear here
```

## 3. Service user and code

```bash
sudo adduser --system --group --shell /bin/bash --home /home/polybot polybot
sudo mkdir -p /opt/polymarketbot
sudo chown polybot:polybot /opt/polymarketbot
```

Copy the project in. From **your Windows PC**, in the project folder:

```powershell
# either: push it over SSH
scp -r . youruser@VPS_IP:/tmp/polymarketbot

# or, if it is in a git remote, clone on the VPS instead
```

Then on the VPS:

```bash
sudo rsync -a --delete \
  --exclude '__pycache__' --exclude '.git' \
  --exclude 'frontend/node_modules' --exclude '.venv' \
  /tmp/polymarketbot/ /opt/polymarketbot/
sudo chown -R polybot:polybot /opt/polymarketbot
```

## 4. Python environment

```bash
sudo -u polybot -H bash -c '
  cd /opt/polymarketbot
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip wheel
  .venv/bin/pip install -r requirements.txt
'
```

Confirm the trading library loaded (this is what the *"Trading library not
loaded"* error in the dashboard refers to):

```bash
sudo -u polybot /opt/polymarketbot/.venv/bin/python -c \
  "import py_clob_client, cryptography; print('ok')"
```

## 5. Build the dashboard

```bash
sudo -u polybot -H bash -c 'cd /opt/polymarketbot/frontend && npm ci && npm run build'
```

On a 1 GB VPS `vite build` can be killed by the OOM reaper. Two ways out —
either add swap:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

…or build on your PC (`cd frontend && npm run build`) and copy only the output:

```powershell
scp -r frontend/dist youruser@VPS_IP:/tmp/dist
```
```bash
sudo rsync -a --delete /tmp/dist/ /opt/polymarketbot/frontend/dist/
sudo chown -R polybot:polybot /opt/polymarketbot/frontend/dist
```

Node is then not needed on the VPS at all.

## 6. Private-key storage

Windows keeps the key in the Credential Manager. A headless VPS has no keyring
daemon, so the app uses an **AES-256-GCM keyfile** unlocked by a passphrase from
the environment (scrypt-derived key; the file is useless without the passphrase).

```bash
sudo cp /opt/polymarketbot/deploy/polymarketbot.env.example /etc/polymarketbot.env
openssl rand -base64 32                      # copy this
sudo nano /etc/polymarketbot.env             # paste as POLYBOT_SECRET_PASSPHRASE
sudo chown root:root /etc/polymarketbot.env
sudo chmod 600 /etc/polymarketbot.env
```

**Back that passphrase up off the VPS.** Lose it and `secret.enc` cannot be
decrypted — you would have to paste the private key into the dashboard again.

Paper trading needs none of this; the file is only touched when you save a key.

> A private key on a rented VPS is only as safe as the VPS. The provider can read
> its disk and memory. Fund the wallet with what you are willing to have on that
> machine, keep the SSH login key-only, and prefer a wallet dedicated to this bot.

## 7. Install the service

```bash
sudo cp /opt/polymarketbot/deploy/polymarketbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now polymarketbot
sudo systemctl status polymarketbot
```

It should report `active (running)`. Verify it is listening — and on localhost
only:

```bash
curl -s http://127.0.0.1:8765/health      # {"ok":true,...}
sudo ss -ltnp | grep 8765                 # must show 127.0.0.1:8765, not 0.0.0.0
```

Logs:

```bash
journalctl -u polymarketbot -f
tail -f /home/polybot/PolymarketBotWeb/bot.log
```

## 8. Open the dashboard

From your Windows PC (PowerShell — `ssh` is built into Windows 11):

```powershell
ssh -N -L 8765:127.0.0.1:8765 youruser@VPS_IP
```

Leave that window open and browse to <http://localhost:8765>.

`-N` means "no shell, just the tunnel". Log in as **your own** SSH user, not
`polybot`. If port 8765 is already used on your PC, forward a different local
one: `-L 9000:127.0.0.1:8765`, then open `http://localhost:9000`.

To keep the tunnel alive through drops, add to `~/.ssh/config` on your PC:

```
Host polybot-vps
    HostName VPS_IP
    User youruser
    LocalForward 8765 127.0.0.1:8765
    ServerAliveInterval 30
    ServerAliveCountMax 3
    ExitOnForwardFailure yes
```

then just `ssh -N polybot-vps`.

## 9. First run on the server

1. Open the dashboard through the tunnel.
2. Go to **Settings** → paste the private key → *Connect & auto-detect account*.
   The credentials panel should report a secure store is present; if it says
   there is none, `POLYBOT_SECRET_PASSPHRASE` did not reach the process — check
   `sudo systemctl show polymarketbot -p EnvironmentFiles` and restart.
3. Balance still `$0.00`? Your USDC is likely in a Polymarket email/browser
   wallet — paste that address into the funder field and re-detect.
4. Run **paper trading** for a session first. Everything above is identical in
   paper mode, so it is a free end-to-end check of the deployment.
5. Live trading arms only when both *Paper trading* and *Dry-run* are off.

---

## Operating it

```bash
sudo systemctl restart polymarketbot     # after a config or code change
sudo systemctl stop polymarketbot        # stops trading; open positions stay open
journalctl -u polymarketbot --since '1 hour ago'
```

`Restart=always` brings the bot back after a crash or VPS reboot. It does **not**
re-arm the engine — the engine's own start/stop state is what the dashboard's
Start button controls, so check the dashboard after an unexpected restart.

**Updating the code:**

```bash
sudo systemctl stop polymarketbot
# rsync/git pull into /opt/polymarketbot, then:
sudo -u polybot -H bash -c 'cd /opt/polymarketbot && .venv/bin/pip install -r requirements.txt'
sudo -u polybot -H bash -c 'cd /opt/polymarketbot/frontend && npm ci && npm run build'
sudo systemctl start polymarketbot
```

Stop the bot first if a position is open, so it is not restarted mid-decision.

**Back up** `/home/polybot/PolymarketBotWeb/` — it holds `copybot.sqlite3` (trade
history), `config.json` (settings), `secret.enc` (the encrypted key) and
`bot.log`:

```bash
sudo tar czf ~/polybot-backup-$(date +%F).tar.gz -C /home/polybot PolymarketBotWeb
```

`secret.enc` in that archive is safe only while the passphrase is not stored
alongside it.

## Troubleshooting

**Service fails instantly.** `journalctl -u polymarketbot -n 50`. Usually the
venv path in the unit file, or missing deps.

**`curl` on 8765 works but the browser shows nothing.** The tunnel is not up, or
you browsed to the VPS IP instead of `localhost`.

**Dashboard loads as raw JSON saying "not built yet".** `frontend/dist` is
missing — redo step 5.

**Port shifted.** `run.py` falls forward to the next free port if 8765 is taken,
so the tunnel would point at nothing. `sudo ss -ltnp | grep uvicorn` shows the
real port; kill the stale process rather than tunnelling to the new one.

**Timestamps/countdowns look wrong.** `sudo timedatectl set-timezone UTC` and
confirm `systemd-timesyncd` is active — settlement timing depends on the clock.

**Clock/expiry filters skipping everything.** Same cause as above; the engine
refuses markets that appear to be already settling.
