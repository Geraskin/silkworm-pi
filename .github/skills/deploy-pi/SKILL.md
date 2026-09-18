---
name: deploy-pi
description: 'Deploy the app to the Raspberry Pi over SSH: upload files with rsync and restart the web server. Use when: deploy/update code on the Pi, upload files via ssh/scp/rsync, restart the camweb server, update silkworm-pi, ship changes to the raspberry pi.'
argument-hint: '[files] — which files to upload (default app_v2.py)'
---

# Deploy to Raspberry Pi

Uploads app files to the Raspberry Pi over SSH and restarts the web server.

## Configuration

Machine-specific values live in **`deploy.env`** next to this file. It is
gitignored, so the repository never contains your own address or key path:

```bash
cp .github/skills/deploy-pi/deploy.env.example .github/skills/deploy-pi/deploy.env
```

Precedence: environment variables → `deploy.env` → the defaults below.

| Variable | Default | Purpose |
|----------|---------|---------|
| `SSH_TARGET` | `pi@raspberrypi.local` | SSH address of the Pi |
| `SSH_KEY` | `~/.ssh/id_ed25519` | private key for the Pi |
| `REMOTE_DIR` | `~/camweb` | app folder on the Pi |
| `APP_FILES` | `app_v2.py camera.py` | files to upload (rsync) |
| `SERVICE_NAME` | `camweb` | systemd service name |
| `HEALTH_URL` | derived from `SSH_TARGET` | health check |

> **Connection note.** A dev container usually cannot resolve `*.local` (mDNS does
> not cross the Docker bridge), so put the Pi's IP in `SSH_TARGET` instead of its
> hostname. The address is DHCP-assigned and can change - re-scan the subnet with
> `nmap -Pn -p 22 --open -sV <your-subnet>/24` if the Pi stops answering. The
> `camweb` systemd service must be installed and enabled (see
> [camweb.service](./assets/camweb.service)), with passwordless sudo for
> `systemctl restart|status|enable camweb`.

Production files are `app_v2.py` (Flask UI) and `camera.py` (picamera2 backend). Do not upload `app.py` (old version).

## Procedure

### 1. Verify SSH access

```bash
ssh -i "${SSH_KEY}" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=5 "${SSH_TARGET}" 'echo ok'
```

If it asks for a password, SSH keys are not forwarded (see `.devcontainer/devcontainer.json`, the `~/.ssh` mount).

### 2. Upload files (rsync)

```bash
rsync -avz -e "ssh -i ${SSH_KEY} -o IdentitiesOnly=yes" ${APP_FILES} "${SSH_TARGET}:${REMOTE_DIR}/"
```

Upload app files only. **Do not use `--delete`** — `~/camweb/` on the Pi contains `settings.json` and `latest.jpg`, which must not be overwritten. If several files changed, list them separated by spaces.

### 3. Restart the server

The `camweb` systemd service is installed (unit: [camweb.service](./assets/camweb.service)). Restart it:

```bash
ssh -i "${SSH_KEY}" -o IdentitiesOnly=yes "${SSH_TARGET}" 'sudo systemctl restart camweb'
```

Fallback without systemd:

```bash
ssh -i "${SSH_KEY}" -o IdentitiesOnly=yes "${SSH_TARGET}" "pkill -f 'python3 .*app_v2' || true; cd ${REMOTE_DIR} && nohup python3 app_v2.py > camweb.log 2>&1 & sleep 1; echo started"
```

### 4. Verify

```bash
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' "${HEALTH_URL}"
```

## Quick start (ready-made script)

```bash
bash .github/skills/deploy-pi/scripts/deploy.sh
```

With overridden parameters:

```bash
SSH_TARGET=pi@192.168.1.50 bash .github/skills/deploy-pi/scripts/deploy.sh
```

The script verifies SSH, uploads files, restarts the server (systemd if available, otherwise fallback) and checks the response.

## Diagnostics

- systemd logs: `ssh -i "${SSH_KEY}" "${SSH_TARGET}" 'sudo journalctl -u camweb -n 50 --no-pager'`
- Is the process alive: `ssh -i "${SSH_KEY}" "${SSH_TARGET}" 'pgrep -af python3'`
- No response on `:8080`: check that the `camweb` service is active and the port is free.

## Important

- Production is `app_v2.py` + `camera.py` only.
- `~/camweb/settings.json` and `~/camweb/latest.jpg` exist only on the Pi — rsync must not touch them.
- systemd commands run via `sudo` (passwordless for camweb).
