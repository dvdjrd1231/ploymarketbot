# Deploying to a Linux VPS

Runs the bot 24/7 under `systemd`, bound to `127.0.0.1`. You reach the dashboard
through an SSH tunnel, so **no port is ever opened to the internet**.

Written for Ubuntu 22.04 / 24.04 and AlmaLinux / Rocky / RHEL 9.

If you want the dashboard reachable at the server's IP in a browser instead, see
[Appendix: public access on port 80](#appendix-public-access-on-port-80).

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
| OS | Ubuntu 22.04+, Debian 12, or AlmaLinux/Rocky/RHEL 9 |
| Python | 3.10+ — Ubuntu 22.04 ships 3.10, 24.04 ships 3.12; **RHEL 9 ships 3.9, so install `python3.12`** |
| Node.js | 18+ — **only to build the dashboard**, not to run the bot |
| Network | outbound HTTPS/WSS to Polymarket; inbound only port 22 |

Pick a region close to Polymarket's infrastructure (US East) — copy trading is
latency-sensitive, and a VPS is usually a real improvement over a home
connection here.

## 2. Base system

### Debian / Ubuntu

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

### AlmaLinux / Rocky / RHEL 9

RHEL 9's `python3` is **3.9 — too old for this project**. Install 3.12 from
AppStream alongside it; do not try to replace the system Python, `dnf` depends
on it.

```bash
sudo dnf update -y
sudo dnf install -y python3.12 python3.12-pip python3.12-devel \
                    git curl gcc gcc-c++ make libffi-devel openssl-devel

python3.12 --version     # must be >= 3.10
```

Node.js, only if you need to rebuild the dashboard (see step 5):

```bash
sudo dnf module reset -y nodejs
sudo dnf module enable -y nodejs:20
sudo dnf install -y nodejs npm
node --version           # must be >= 18
```

Firewall — `firewalld` here, not `ufw`:

```bash
sudo firewall-cmd --permanent --add-service=ssh
sudo firewall-cmd --reload
sudo firewall-cmd --list-all       # 8765 must NOT be listed
```

**SELinux** is enforcing by default. A plain systemd unit like ours runs
unconfined and needs no policy work, but if the service fails for no visible
reason, confirm it is not SELinux before hunting further:

```bash
sudo ausearch -m avc -ts recent          # empty means SELinux is not the cause
sudo restorecon -Rv /opt/ploymarketbot   # fixes contexts after a manual copy
```

## 3. Service user and code

`adduser` is Debian-only; on RHEL use `useradd`. `-m` matters — a system account
gets no home directory otherwise, and the database, log and encrypted keyfile all
live in it.

```bash
# RHEL / AlmaLinux
sudo useradd -r -m -d /home/polybot -s /bin/bash polybot

# Debian / Ubuntu
sudo adduser --system --group --shell /bin/bash --home /home/polybot polybot

sudo mkdir -p /opt/ploymarketbot
sudo chown polybot:polybot /opt/ploymarketbot
```

Get the project in. Either clone it on the VPS:

```bash
sudo -u polybot git clone <your-repo-url> /opt/ploymarketbot
```

…or push it from **your Windows PC**, in the project folder:

```powershell
scp -r . youruser@VPS_IP:/tmp/polymarketbot
```

```bash
sudo rsync -a --delete \
  --exclude '__pycache__' --exclude '.git' \
  --exclude 'frontend/node_modules' --exclude '.venv' \
  /tmp/polymarketbot/ /opt/ploymarketbot/
sudo chown -R polybot:polybot /opt/ploymarketbot
```

> **If your clone predates the repo's `.gitignore`**, it contains a committed
> `frontend/node_modules` built on Windows — including native binaries such as
> `@rollup/rollup-win32-x64-msvc`, which cannot run on Linux and will make
> `npm run build` fail in confusing ways. Delete it before doing anything else:
>
> ```bash
> sudo rm -rf /opt/ploymarketbot/frontend/node_modules
> sudo find /opt/ploymarketbot -name '__pycache__' -type d -exec rm -rf {} +
> ```

## 4. Python environment

Use the interpreter that is actually 3.10+. On RHEL/AlmaLinux that is
`python3.12`; on Ubuntu plain `python3` is fine.

```bash
sudo -u polybot -H bash -c '
  cd /opt/ploymarketbot
  python3.12 -m venv .venv          # Ubuntu: python3 -m venv .venv
  .venv/bin/pip install --upgrade pip wheel
  .venv/bin/pip install -r requirements.txt
'
```

Once the venv exists, the interpreter inside it is always `.venv/bin/python`
regardless of which Python created it — so the systemd unit needs no change.

Confirm the trading library loaded (this is what the *"Trading library not
loaded"* error in the dashboard refers to):

```bash
sudo -u polybot /opt/ploymarketbot/.venv/bin/python -c \
  "import py_clob_client, cryptography; print('ok')"
```

## 5. Build the dashboard

First check whether you even need to. The build output is plain JS/CSS with no
platform-specific parts, so if `frontend/dist` came with your clone it works as
it is and Node is not required on the VPS at all:

```bash
ls -l /opt/ploymarketbot/frontend/dist/index.html \
      /opt/ploymarketbot/frontend/dist/assets/
```

If that is present, skip to step 6. To build (or rebuild after a UI change):

```bash
sudo -u polybot -H bash -c 'cd /opt/ploymarketbot/frontend && npm ci && npm run build'
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
sudo rsync -a --delete /tmp/dist/ /opt/ploymarketbot/frontend/dist/
sudo chown -R polybot:polybot /opt/ploymarketbot/frontend/dist
```

Node is then not needed on the VPS at all.

## 6. Private-key storage

Windows keeps the key in the Credential Manager. A headless VPS has no keyring
daemon, so the app uses an **AES-256-GCM keyfile** unlocked by a passphrase from
the environment (scrypt-derived key; the file is useless without the passphrase).

```bash
sudo cp /opt/ploymarketbot/deploy/polymarketbot.env.example /etc/polymarketbot.env
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
sudo cp /opt/ploymarketbot/deploy/polymarketbot.service /etc/systemd/system/
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
# rsync/git pull into /opt/ploymarketbot, then:
sudo -u polybot -H bash -c 'cd /opt/ploymarketbot && .venv/bin/pip install -r requirements.txt'
sudo -u polybot -H bash -c 'cd /opt/ploymarketbot/frontend && npm ci && npm run build'
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
venv path in the unit file, a `WorkingDirectory` that does not match where you
actually cloned, or missing deps. On AlmaLinux also rule out SELinux
(`sudo ausearch -m avc -ts recent`).

**`SyntaxError` or `ImportError` on startup, RHEL/AlmaLinux.** The venv was built
with the system Python 3.9. Rebuild it: `rm -rf .venv` and redo step 4 with
`python3.12`.

**`npm run build` fails with a rollup or esbuild platform error.** A Windows
`node_modules` came along with the clone. `rm -rf frontend/node_modules` and
`npm ci` again — see the note in step 3.

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

---

# Appendix: running with Docker

An alternative to steps 2–8 above, not an addition — pick one. Docker avoids
installing Python 3.12 and Node on the host entirely, which on RHEL 9 (system
Python 3.9) is the awkward part.

**Do not run both.** The systemd service and the container would fight over the
port: `sudo systemctl disable --now polymarketbot` first.

## 1. Install Docker

```bash
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
docker --version
```

Podman is fine too — `dnf install -y podman podman-compose`, then substitute
`podman-compose` for `docker compose`. The Dockerfile needs no changes.

## 2. Configure

```bash
cd /opt/ploymarketbot
cp deploy/docker.env.example .env
openssl rand -base64 32          # paste into .env
nano .env
chmod 600 .env
```

`.env` is gitignored and never enters an image layer (see `.dockerignore`).

By default `docker-compose.yml` publishes on **port 80**. For localhost-only
plus an SSH tunnel, swap the `ports:` entry for the commented
`127.0.0.1:8765:8765` line.

## 3. Build and start

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f
```

The first build takes a few minutes — it compiles the dashboard and the Python
wheels. Later builds reuse cached layers unless `requirements.txt` or
`frontend/package.json` change.

```bash
sudo firewall-cmd --permanent --add-port=80/tcp && sudo firewall-cmd --reload
curl -s http://127.0.0.1/health
```

`docker compose ps` should show `healthy` after ~20 seconds; that is the
container polling `/health` itself.

## Day-to-day

```bash
docker compose restart
docker compose down                    # stop; the volume survives
docker compose up -d --build           # after a code change
docker compose logs -f --tail=100
docker compose exec bot sh             # shell inside the container
```

## Your data lives in a volume

The database, `config.json`, `bot.log` and `secret.enc` are on the `botdata`
volume, mounted at `/data`. Rebuilding the image does not touch it.

```bash
docker volume inspect polymarketbot_botdata          # where it is on disk
docker run --rm -v polymarketbot_botdata:/data -v "$PWD":/backup alpine \
  tar czf /backup/polybot-backup.tar.gz -C /data .   # back it up
```

`docker compose down -v` **deletes** that volume, and with it your trade history
and stored key. Use plain `down`.

## Notes specific to this app

- `keyring` has no backend inside a container, so the app uses the
  passphrase-encrypted keyfile. That is why `POLYBOT_SECRET_PASSPHRASE` is
  required for live trading here; without it the credentials screen reports no
  secure store.
- The container runs as a non-root user and binds 8765 internally; Docker maps
  host 80 to it, so no privileged-port capability is involved.
- `run.py` is bypassed — the image calls `uvicorn` directly with the same
  WebSocket ping settings. The dashboard is prebuilt into the image, so there is
  nothing for `run.py`'s npm logic to do.
- `TZ=UTC` is set in compose: the engine's expiry and settlement filters compare
  against the clock.

---

# Appendix: public access on port 80

Makes `http://<server-ip>/` work in any browser, with no SSH tunnel.

**Understand what this exposes.** There is no login on this API. Anyone who
reaches the port can:

| Route | What a stranger can do |
|---|---|
| `POST /api/positions/close` | dump every open position at market |
| `PUT /api/settings` | retarget the bot at a wallet they control, so it copies deliberately losing trades |
| `POST /api/engine/*` | start, stop or restart trading |
| `DELETE /api/credentials` | wipe your stored key |
| `POST /api/session/reset` | erase trade history and P/L |

They **cannot** read the private key back — no route returns it — and there is no
withdraw or transfer route, so USDC cannot be moved out directly. The exposure is
trade manipulation and data loss, which with a funded wallet is still real money.

## 1. Bind port 80

A drop-in override keeps the shipped unit file intact, so reverting is one
deletion.

```bash
sudo mkdir -p /etc/systemd/system/polymarketbot.service.d
sudo cp /opt/ploymarketbot/deploy/public-port80.conf \
        /etc/systemd/system/polymarketbot.service.d/
sudo systemctl daemon-reload
sudo systemctl restart polymarketbot
```

Port 80 is privileged, so the override grants the service `CAP_NET_BIND_SERVICE`
rather than running it as root.

Check nothing else already holds port 80 (`sudo ss -ltnp | grep ':80 '`). If
something does, `run.py` silently falls forward to 81 and the URL will not work —
stop the other service first.

## 2. Open the firewall

Recommended — your IP only. It still gives you browser access at the IP while
keeping everyone else out, which costs nothing extra:

```bash
MYIP=$(curl -s https://ifconfig.me)          # run this from the VPS
sudo firewall-cmd --permanent --add-rich-rule="rule family=ipv4 source address=${MYIP}/32 port port=80 protocol=tcp accept"
sudo firewall-cmd --reload
```

Re-run it when your home IP changes. Or, open it to everyone:

```bash
sudo firewall-cmd --permanent --add-port=80/tcp
sudo firewall-cmd --reload
```

## 3. Verify

```bash
sudo ss -ltnp | grep ':80 '        # expect 0.0.0.0:80
curl -s http://127.0.0.1/health
sudo firewall-cmd --list-all
```

Then browse to `http://<server-ip>/` — no port needed, 80 is the default.

## Reverting to localhost-only

```bash
sudo rm /etc/systemd/system/polymarketbot.service.d/public-port80.conf
sudo systemctl daemon-reload && sudo systemctl restart polymarketbot
sudo firewall-cmd --permanent --remove-port=80/tcp
sudo firewall-cmd --reload
```

## Worth doing if this stays public

- Keep **paper trading on** and no private key stored while the port is open.
- Add TLS and a password — ask for the nginx + Let's Encrypt + Basic auth config;
  it is about ten minutes of work and removes most of the table above.
- `sudo dnf install -y fail2ban && sudo systemctl enable --now fail2ban`.
- Watch `/api` hits you did not make: `journalctl -u polymarketbot -f`.
