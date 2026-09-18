#!/usr/bin/env bash
set -euo pipefail

# Deploy to the Raspberry Pi over SSH: rsync + server restart.
# Parameters can be overridden via environment variables.

SSH_TARGET="${SSH_TARGET:-alexey@192.168.10.105}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/silkworm-pi}"
REMOTE_DIR="${REMOTE_DIR:-~/camweb}"
APP_FILES="${APP_FILES:-app_v2.py camera.py}"
MAIN_MODULE="${MAIN_MODULE:-app_v2.py}"
SERVICE_NAME="${SERVICE_NAME:-camweb}"
HEALTH_URL="${HEALTH_URL:-http://192.168.10.105:8080/}"

SSH_OPTS=(-i "${SSH_KEY}" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=5)

echo "==> SSH access: ${SSH_TARGET}"
ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" 'echo ok'

echo "==> Uploading: ${APP_FILES} -> ${REMOTE_DIR}"
# shellcheck disable=SC2086
rsync -avz -e "ssh -i '${SSH_KEY}' -o IdentitiesOnly=yes" ${APP_FILES} "${SSH_TARGET}:${REMOTE_DIR}/"

echo "==> Restarting server"
if ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" \
    "systemctl list-unit-files '${SERVICE_NAME}.service' >/dev/null 2>&1"; then
    echo "   systemd: restart ${SERVICE_NAME}"
    ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "sudo systemctl restart '${SERVICE_NAME}'"
else
    echo "   fallback: pkill + nohup"
    ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" \
        "pkill -f 'python3 .*${MAIN_MODULE}' || true; cd '${REMOTE_DIR}' && nohup python3 '${MAIN_MODULE}' > camweb.log 2>&1 & sleep 1; echo started"
fi

echo "==> Checking: ${HEALTH_URL}"
# The app needs a few seconds to start (camera init), so retry on connection refused.
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' \
    --retry 8 --retry-delay 2 --retry-connrefused --connect-timeout 3 \
    "${HEALTH_URL}" || echo 'WARN: server not responding yet'
