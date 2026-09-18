#!/usr/bin/env bash
set -euo pipefail

# Deploy to the Raspberry Pi over SSH: rsync + server restart.
#
# Settings are resolved in this order:
#   1. environment variables,
#   2. deploy.env next to this skill (gitignored, machine-specific),
#   3. the generic defaults below.
# Copy deploy.env.example to deploy.env and fill in your own values.

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${DEPLOY_ENV:-${SKILL_DIR}/deploy.env}"
if [[ -f "${ENV_FILE}" ]]; then
    echo "==> Config: ${ENV_FILE}"
    # shellcheck disable=SC1090
    set -a
    source "${ENV_FILE}"
    set +a
fi

SSH_TARGET="${SSH_TARGET:-pi@raspberrypi.local}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${REMOTE_DIR:-~/camweb}"
APP_FILES="${APP_FILES:-app_v2.py camera.py}"
MAIN_MODULE="${MAIN_MODULE:-app_v2.py}"
SERVICE_NAME="${SERVICE_NAME:-camweb}"

# Derive the host from the SSH target: drop "user@" and any ":port".
SSH_HOST="${SSH_TARGET#*@}"
SSH_HOST="${SSH_HOST%%:*}"
HEALTH_URL="${HEALTH_URL:-http://${SSH_HOST}:8080/}"

if [[ "${SSH_TARGET}" == "pi@raspberrypi.local" && ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: SSH_TARGET is not set and ${ENV_FILE} does not exist." >&2
    echo "       cp ${SKILL_DIR}/deploy.env.example ${ENV_FILE}" >&2
    echo "       then put your Pi's address in it." >&2
    exit 1
fi

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
