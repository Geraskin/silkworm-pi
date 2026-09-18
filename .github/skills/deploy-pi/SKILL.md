---
name: deploy-pi
description: 'Deploy the app to the Raspberry Pi over SSH: upload files with rsync and restart the web server. Use when: deploy/update code on the Pi, upload files via ssh/scp/rsync, restart the camweb server, update silkworm-pi, ship changes to the raspberry pi.'
argument-hint: '[files] — which files to upload (default app_v2.py)'
---

# Deploy to Raspberry Pi

Uploads app files to the Raspberry Pi over SSH and restarts the web server.

## Parameters (defaults)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SSH_TARGET` | `alexey@192.168.10.105` | SSH address (DHCP IP, see note) |
| `SSH_KEY` | `~/.ssh/silkworm-pi` | private key for the Pi |
| `REMOTE_DIR` | `~/camweb` | app folder on the Pi |
| `APP_FILES` | `app_v2.py camera.py` | files to upload (rsync) |
| `SERVICE_NAME` | `camweb` | systemd service name |
| `HEALTH_URL` | `http://192.168.10.105:8080/` | health check |

> **Connection note.** The dev container cannot resolve `*.local` (mDNS does not
> cross the Docker bridge), so connect by IP with the dedicated key:
> `ssh -i ~/.ssh/silkworm-pi alexey@192.168.10.105`. The Pi's IP is DHCP-assigned
> and can change — re-scan with `nmap -Pn -p 22 --open -sV 192.168.10.0/24` if
> `192.168.10.105` stops answering. The `camweb` systemd service is installed and
> sudo is passwordless for `systemctl restart|status|enable camweb`.

Production files are `app_v2.py` (Flask UI) and `camera.py` (picamera2 backend). Do not upload `app.py` (old version).

## Procedure

### 1. Verify SSH access

```bash
ssh -i ~/.ssh/silkworm-pi -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=5 alexey@192.168.10.105 'echo ok'
```

If it asks for a password, SSH keys are not forwarded (see `.devcontainer/devcontainer.json`, the `~/.ssh` mount).

### 2. Upload files (rsync)

```bash
rsync -avz -e "ssh -i ~/.ssh/silkworm-pi -o IdentitiesOnly=yes" app_v2.py camera.py alexey@192.168.10.105:~/camweb/
```

Upload app files only. **Do not use `--delete`** — `~/camweb/` on the Pi contains `settings.json` and `latest.jpg`, which must not be overwritten. If several files changed, list them separated by spaces.

### 3. Restart the server

The `camweb` systemd service is installed (unit: [camweb.service](./assets/camweb.service)). Restart it:

```bash
ssh -i ~/.ssh/silkworm-pi -o IdentitiesOnly=yes alexey@192.168.10.105 'sudo systemctl restart camweb'
```

Fallback without systemd:

```bash
ssh -i ~/.ssh/silkworm-pi -o IdentitiesOnly=yes alexey@192.168.10.105 "pkill -f 'python3 .*app_v2' || true; cd ~/camweb && nohup python3 app_v2.py > camweb.log 2>&1 & sleep 1; echo started"
```

### 4. Verify

```bash
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://192.168.10.105:8080/
```

## Quick start (ready-made script)

```bash
bash .github/skills/deploy-pi/scripts/deploy.sh
```

With overridden parameters:

```bash
SSH_TARGET=alexey@192.168.10.105 bash .github/skills/deploy-pi/scripts/deploy.sh
```

The script verifies SSH, uploads files, restarts the server (systemd if available, otherwise fallback) and checks the response.

## Diagnostics

- systemd logs: `ssh -i ~/.ssh/silkworm-pi alexey@192.168.10.105 'sudo journalctl -u camweb -n 50 --no-pager'`
- Is the process alive: `ssh -i ~/.ssh/silkworm-pi alexey@192.168.10.105 'pgrep -af python3'`
- No response on `:8080`: check that the `camweb` service is active and the port is free.

## Important

- Production is `app_v2.py` + `camera.py` only.
- `~/camweb/settings.json` and `~/camweb/latest.jpg` exist only on the Pi — rsync must not touch them.
- systemd commands run via `sudo` (passwordless for camweb).
