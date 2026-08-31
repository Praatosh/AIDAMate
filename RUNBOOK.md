# AIDA-MATE — Runbook

Step-by-step commands to start (and stop) everything: Docker Desktop, the
app container, and the ngrok tunnel that lets Linear's webhooks reach it.
Written so you can run this without me — copy-paste in order, top to bottom.

Shell: **Windows PowerShell**, run from the `gitmate/` project folder unless
noted otherwise.

---

## Quick start (everything already built)

The four commands that get you from "nothing running" to "fully live":

```powershell
cd <path-to-this-repo>

# 1. Start Docker Desktop if it isn't already running (skip if it is)
Start-Process "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"

# 2. Wait ~15-30s for Docker to finish starting, then start the app container
docker compose up -d

# 3. Start the ngrok tunnel (same reserved domain every time)
Start-Process -FilePath "ngrok" -ArgumentList "http","--url=https://napping-abdomen-precision.ngrok-free.dev","8000" -WindowStyle Hidden

# 4. Verify (see "Verify everything is working" below)
```

That's it for the common case. Read on for details, first-time setup, and
what to do when something doesn't come up cleanly.

---

## 1. Start Docker Desktop

Docker Desktop is a background app, not a command that returns — it needs a
short wait after launching before `docker` commands will work.

```powershell
Start-Process "$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe"
```

Check whether it's actually ready before moving on:

```powershell
docker version
```

If this returns real version info (not an error), Docker is ready. If it
errors, wait 10-15 seconds and try again — Docker Desktop takes a moment to
finish starting its engine after the window appears.

**If Docker Desktop won't start at all** (a red "Virtualization support not
detected" screen): see [Troubleshooting](#troubleshooting) below — this is a
BIOS-level setting, not something a command fixes.

---

## 2. Start the app container

From the `gitmate/` folder:

```powershell
cd <path-to-this-repo>
docker compose up -d
```

`-d` runs it in the background (detached) instead of holding the terminal.
This uses whatever image was last built — it does **not** pick up code
changes. See [Rebuilding after a code change](#rebuilding-after-a-code-change)
if you've edited anything in `app/`.

Confirm it's actually running and healthy:

```powershell
docker ps --filter "name=aida-mate" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

You want to see `Up ... (healthy)`. If it says `(health: starting)`, wait a
few more seconds and check again — the container's own healthcheck needs a
moment to run.

---

## 3. Start the ngrok tunnel

```powershell
Start-Process -FilePath "ngrok" -ArgumentList "http","--url=https://napping-abdomen-precision.ngrok-free.dev","8000" -WindowStyle Hidden
```

This is the **reserved static domain** for the ngrok account currently
authenticated on this machine — always use this exact domain so you never
have to touch Linear's configuration again. Port `8000` is where the
container publishes itself (see `docker-compose.yml`).

**Note:** this domain changed on 2026-08-19 (the previous domain,
`hazily-ibuprofen-wildcat.ngrok-free.dev`, belongs to a different ngrok
account than the authtoken configured here). If Linear's app settings still
point at the old domain, the webhook URL there needs updating to
`https://napping-abdomen-precision.ngrok-free.dev/webhooks/linear`.

**If ngrok is already running**, starting a second instance against the same
reserved domain will fail (the free tier allows one tunnel per domain at a
time). Check first:

```powershell
Get-Process ngrok -ErrorAction SilentlyContinue
```

If that returns a process, the tunnel is very likely already up — skip to
verification below instead of starting a new one.

---

## Verify everything is working

Three checks, in order of how much they prove:

```powershell
# 1. Container is running and healthy
docker ps --filter "name=aida-mate" --format "{{.Names}}: {{.Status}}"

# 2. App responds locally
Invoke-RestMethod -Uri "http://localhost:8000/ready" | ConvertTo-Json

# 3. App responds through the public tunnel (this is what Linear actually hits)
Invoke-WebRequest -Uri "https://napping-abdomen-precision.ngrok-free.dev/health" `
  -Headers @{"ngrok-skip-browser-warning"="1"} -UseBasicParsing | Select-Object StatusCode, Content
```

Expect: `(healthy)`, then `{"status":"ready","sandbox":true,"github":true,"linear":true}`,
then `StatusCode 200` with `{"status":"ok"}`. If all three come back clean,
Linear webhooks can reach the app.

---

## Checking logs

```powershell
# Follow live logs
docker compose logs -f

# Last 50 lines only
docker logs aida-mate --tail 50
```

Every line is structured JSON — `event` fields like `REVIEW_CREATED`,
`AGENT_STARTED`, `RISK_CLASSIFIED` mark the review pipeline's progress
through a single review.

---

## Rebuilding after a code change

`docker compose up -d` alone reuses the existing image — it will **not**
pick up edits to anything under `app/`. After changing code:

```powershell
docker compose up -d --build
```

This rebuilds only the changed layers (fast if only `app/` changed, since
dependencies are cached separately) and restarts the container with the new
image. Review history and Linear OAuth installs are untouched — they live in
the named volume, not the image.

---

## Stopping everything

```powershell
# Stop the app container (keeps the image and the data volume)
docker compose down

# Stop the ngrok tunnel
Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
```

`docker compose down` does **not** delete review history — that lives in the
named volume (`gitmate_aida-mate-data`), which survives until you explicitly
remove it:

```powershell
docker compose down -v   # ALSO deletes the data volume — only if you mean it
```

---

## Troubleshooting

**`docker version` errors, or Docker Desktop shows "Virtualization support
not detected"** — this is a BIOS/firmware setting (Intel VT-x), not
something fixable from a terminal. Check what's actually happening:

```powershell
systeminfo | Select-String -Pattern "Hyper-V|Virtualization"
```

If it reports `Virtualization Enabled In Firmware: No`, you need to enable
it in the BIOS (restart → F1 on this Lenovo ThinkPad → Security →
Virtualization → enable Intel VT-x → F10 to save). If that setting is
greyed out or password-protected, your organization's IT/device management
has locked it and needs to make the change.

**`docker compose up` fails to bind port 8000** — something else is already
using it, most often a leftover bare `uvicorn` process from testing without
Docker:

```powershell
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
```

**ngrok tunnel starts but the public URL 404s or won't connect** — almost
always means the app container isn't actually running yet. Check `docker ps`
first; start the container before the tunnel, not after.

**Container shows `Up ... (unhealthy)`** — check what's actually failing:

```powershell
docker logs aida-mate --tail 50
```

Common cause: a bad or missing value in `.env` (the container reads it via
`env_file:` in `docker-compose.yml` — it needs a real `.env` file present in
this folder, copied from `.env.example` and filled in, same as running
without Docker).

---

## Reference

| What | Value |
|---|---|
| Project folder | wherever you cloned this repo |
| App container name | `aida-mate` |
| Local URL | `http://localhost:8000` |
| Public URL (ngrok) | `https://napping-abdomen-precision.ngrok-free.dev` |
| Data volume | `gitmate_aida-mate-data` (SQLite review store + Linear OAuth installs) |
| Docker Desktop path | `$env:LOCALAPPDATA\Programs\DockerDesktop\Docker Desktop.exe` |

See also: [README.md](README.md) (setup, config, endpoints),
[ARCHITECTURE.md](ARCHITECTURE.md) (design), [ROADMAP.md](ROADMAP.md)
(build history + backlog).
