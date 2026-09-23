#!/usr/bin/env bash
#
# Verify the app on the Raspberry Pi over SSH and HTTP.
#
# Everything that can be checked without eyes is checked here: the web server,
# a short timelapse, the local frames and state.json, the NAS cache/rotation,
# and - with --reboot - that an interrupted run resumes instead of starting over.
#
# Settings come from deploy.env next to this skill, exactly like deploy.sh:
#   1. environment variables, 2. deploy.env, 3. the defaults below.
#
# Usage:
#   bash .github/skills/deploy-pi/scripts/smoke-test.sh [options]
#
#   --deploy            run deploy.sh first
#   --reboot            also test resuming across a real reboot (the Pi goes down)
#   --interval N        timelapse interval in seconds for the test (default 5)
#   --frames N          how many frames to wait for (default 4)
#   --keep-running      leave the test timelapse running when finished
#   -h, --help          this text
#
# Exit code is 0 only when every check passed.

set -uo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${DEPLOY_ENV:-${SKILL_DIR}/deploy.env}"
if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    set -a
    source "${ENV_FILE}"
    set +a
fi

SSH_TARGET="${SSH_TARGET:-pi@raspberrypi.local}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
REMOTE_DIR="${REMOTE_DIR:-~/camweb}"
SERVICE_NAME="${SERVICE_NAME:-camweb}"

SSH_HOST="${SSH_TARGET#*@}"
SSH_HOST="${SSH_HOST%%:*}"
HEALTH_URL="${HEALTH_URL:-http://${SSH_HOST}:8080/}"
BASE="${HEALTH_URL%/}"

if [[ "${SSH_TARGET}" == "pi@raspberrypi.local" && ! -f "${ENV_FILE}" ]]; then
    echo "ERROR: SSH_TARGET is not set and ${ENV_FILE} does not exist." >&2
    echo "       cp ${SKILL_DIR}/deploy.env.example ${ENV_FILE} and fill it in." >&2
    exit 2
fi

DO_DEPLOY=0
DO_REBOOT=0
KEEP_RUNNING=0
INTERVAL=5
WANT_FRAMES=4

while [[ $# -gt 0 ]]; do
    case "$1" in
        --deploy) DO_DEPLOY=1 ;;
        --reboot) DO_REBOOT=1 ;;
        --keep-running) KEEP_RUNNING=1 ;;
        --interval) INTERVAL="${2:?--interval needs a value}"; shift ;;
        --frames) WANT_FRAMES="${2:?--frames needs a value}"; shift ;;
        -h|--help) awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

SSH_OPTS=(-i "${SSH_KEY}" -o IdentitiesOnly=yes -o BatchMode=yes -o ConnectTimeout=8)
RESULTS=()
say()  { printf '%s\n' "$*"; }
pass() { RESULTS+=("PASS|$1|${2:-}"); }
fail() { RESULTS+=("FAIL|$1|${2:-}"); }
skip() { RESULTS+=("SKIP|$1|${2:-}"); }

remote() { ssh "${SSH_OPTS[@]}" "${SSH_TARGET}" "$@"; }
api()    { curl -fsS --max-time 15 "${BASE}$1" "${@:2}"; }
# grep -c exits 1 when it counts nothing, so count without a fallback of our own
count_remote() { remote "$1" 2>/dev/null | head -1 | tr -dc '0-9'; }
field()  { python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get(sys.argv[1], ""))
except Exception:
    print("")' "$1"; }
status_field() { api /timelapse/state 2>/dev/null | field "$1"; }

# ---------------------------------------------------------------- preflight
say "==> Target: ${SSH_TARGET} (${BASE})"

if [[ "${DO_DEPLOY}" == "1" ]]; then
    say "==> Deploying first"
    bash "${SKILL_DIR}/scripts/deploy.sh" || { echo "deploy failed" >&2; exit 2; }
fi

if ! remote 'echo ok' >/dev/null 2>&1; then
    echo "ERROR: cannot reach ${SSH_TARGET} over SSH." >&2
    exit 2
fi
pass "SSH reachable"

if remote "systemctl is-active '${SERVICE_NAME}'" 2>/dev/null | grep -q '^active$'; then
    pass "service ${SERVICE_NAME} is active"
else
    fail "service ${SERVICE_NAME} is active" "not active"
fi

code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "${BASE}/" || true)
[[ "${code}" == "200" ]] && pass "web UI answers on ${BASE}" \
                         || fail "web UI answers on ${BASE}" "HTTP ${code}"

free_pct=$(status_field free_percent)
say "    free space on the card: ${free_pct:-?}%"
say "    cpu: $(remote 'vcgencmd measure_temp 2>/dev/null' || echo n/a)"

# NAS configuration, as the app sees it
nas_enabled=$(status_field nas_enabled)
nas_ready=$(status_field nas_ready)
nas_reason=$(status_field nas_reason)
if [[ "${nas_enabled}" == "True" ]]; then
    say "    NAS enabled, ready=${nas_ready} ${nas_reason:+(${nas_reason})}"
else
    say "    NAS export is switched off in settings.json"
fi

# ---------------------------------------------------------------- timelapse
was_active=$(status_field active)
if [[ "${was_active}" == "True" ]]; then
    say "==> A timelapse was already running; using it and leaving it alone"
    KEEP_RUNNING=1
else
    say "==> Starting a timelapse: one frame every ${INTERVAL}s"
    api "/timelapse/start" -X POST -d "interval_s=${INTERVAL}" >/dev/null \
        || say "    WARN: start request failed"
fi

started_frames=$(status_field frames)
say "    waiting for ${WANT_FRAMES} frames…"
waited=0
frames="${started_frames:-0}"
while (( waited < INTERVAL * WANT_FRAMES + 45 )); do
    frames=$(status_field frames); frames="${frames:-0}"
    (( frames >= WANT_FRAMES )) && break
    sleep 2; waited=$((waited + 2))
done

session=$(status_field session)
if (( frames >= WANT_FRAMES )); then
    pass "timelapse captured ${frames} frames" "session ${session}"
else
    fail "timelapse captured ${frames} frames" "wanted ${WANT_FRAMES} in $((INTERVAL * WANT_FRAMES + 45))s"
fi
say "    last_error: '$(status_field last_error)'"

# the frames on the card must match what the state file claims
local_jpg=$(count_remote "ls -1 ${REMOTE_DIR}/timelapse/${session}/ 2>/dev/null | grep -c 'frame_.*\.jpg$' || true")
local_jpg=${local_jpg:-0}
if (( local_jpg == frames )); then
    pass "frame files on the card match the counter" "${local_jpg} jpg"
else
    fail "frame files on the card match the counter" "state says ${frames}, disk has ${local_jpg}"
fi

# numbering must be contiguous, so a resume can never duplicate or skip
if (( frames > 0 )); then
    gaps=$(count_remote "ls -1 ${REMOTE_DIR}/timelapse/${session}/ 2>/dev/null | grep -o 'frame_[0-9]*' | sort -u | sed 's/frame_//' | sort -n | awk 'NR>1 && \$1!=prev+1 {c++} {prev=\$1} END {print c+0}'")
    gaps=${gaps:-0}
    [[ "${gaps}" == "0" ]] && pass "frame numbering is contiguous" \
                           || fail "frame numbering is contiguous" "${gaps} gap(s)"
else
    skip "frame numbering is contiguous" "no frames were taken"
fi

if remote "python3 -c 'import json,os;json.load(open(os.path.expanduser(\"${REMOTE_DIR}/timelapse/state.json\")))'" >/dev/null 2>&1; then
    pass "state.json is valid JSON on the Pi"
else
    fail "state.json is valid JSON on the Pi"
fi

raw_enabled=$(remote "python3 -c 'import json,os;print(json.load(open(os.path.expanduser(\"${REMOTE_DIR}/settings.json\"))).get(\"save_raw\"))'" 2>/dev/null || echo "?")
if [[ "${raw_enabled}" == "True" ]]; then
    local_dng=$(count_remote "ls -1 ${REMOTE_DIR}/timelapse/${session}/ 2>/dev/null | grep -c 'frame_.*\.dng$' || true")
    local_dng=${local_dng:-0}
    (( local_dng == frames )) && pass "RAW frames written for every frame" "${local_dng} dng" \
                               || fail "RAW frames written for every frame" "${local_dng} dng for ${frames} frames"
else
    skip "RAW frames written for every frame" "save_raw is off"
fi

# ---------------------------------------------------------------- NAS
if [[ "${nas_enabled}" == "True" && "${nas_ready}" == "True" ]]; then
    say "==> NAS is mounted; waiting for the sweep to catch up"
    sleep 12
    nas_dir=$(remote "python3 -c 'import json,os;print(json.load(open(os.path.expanduser(\"${REMOTE_DIR}/settings.json\"))).get(\"nas_dir\",\"\"))'" 2>/dev/null || echo "")
    nas_jpg=$(count_remote "ls -1 '${nas_dir}'/${session}/ 2>/dev/null | grep -c 'frame_.*\.jpg$' || true")
    nas_jpg=${nas_jpg:-0}
    if (( nas_jpg >= frames )); then
        pass "frames reached the NAS" "${nas_jpg} jpg in ${nas_dir}/${session}"
    else
        fail "frames reached the NAS" "${nas_jpg} of ${frames} in ${nas_dir}/${session}"
    fi
    pending=$(status_field nas_pending); pending="${pending:-0}"
    (( pending == 0 )) && pass "nothing is left queued" \
                       || fail "nothing is left queued" "${pending} file(s) pending"
    # copies are kept once they are on the NAS: that is the whole point of the cache
    (( local_jpg >= frames )) && pass "local copies are kept, not moved" \
                              || fail "local copies are kept, not moved" "${local_jpg} of ${frames}"
elif [[ "${nas_enabled}" == "True" ]]; then
    say "==> NAS is enabled but not usable (${nas_reason}); checking that nothing is lost"
    nas_dir=$(remote "python3 -c 'import json,os;print(json.load(open(os.path.expanduser(\"${REMOTE_DIR}/settings.json\"))).get(\"nas_dir\",\"\"))'" 2>/dev/null || echo "")
    say "    configured folder: ${nas_dir:-none}"
    mount_info=$(remote "findmnt -no TARGET,FSTYPE --target '${nas_dir:-/}'" 2>/dev/null || true)
    say "    mount check on the Pi: ${mount_info:-not a mount point}"
    before=$(remote "find ${REMOTE_DIR}/timelapse -name 'frame_*' -type f 2>/dev/null | wc -l" 2>/dev/null || echo 0)
    sleep 10
    after=$(remote "find ${REMOTE_DIR}/timelapse -name 'frame_*' -type f 2>/dev/null | wc -l" 2>/dev/null || echo 0)
    if (( after >= before )); then
        pass "an unusable NAS never deletes frames" "${before} -> ${after} files"
    else
        fail "an unusable NAS never deletes frames" "${before} -> ${after} files"
    fi
    say "    reason reported by the app: ${nas_reason}"
else
    skip "NAS round trip" "export is switched off"
fi

# ---------------------------------------------------------------- reboot resume
if [[ "${DO_REBOOT}" == "1" ]]; then
    say "==> Testing the resume across a reboot (the Pi is going down now)"
    frames_before=$(status_field frames); frames_before="${frames_before:-0}"
    say "    frames before: ${frames_before}"

    remote 'sudo -n systemctl reboot' >/dev/null 2>&1 || say "    WARN: reboot command failed"
    sleep 5
    down_start=$(date +%s)
    for _ in $(seq 1 60); do
        remote 'echo ok' >/dev/null 2>&1 && break
        sleep 3
    done
    if ! remote 'echo ok' >/dev/null 2>&1; then
        fail "the Pi came back after the reboot" "no SSH after 3 minutes"
    else
        downtime=$(( $(date +%s) - down_start ))
        pass "the Pi came back after the reboot" "${downtime}s down"

        for _ in $(seq 1 40); do
            curl -fsS --max-time 5 "${BASE}/timelapse/state" >/dev/null 2>&1 && break
            sleep 3
        done
        resumed=$(status_field active)
        [[ "${resumed}" == "True" ]] && pass "the run is active again after the reboot" \
                                     || fail "the run is active again after the reboot" "active=${resumed}"
        session_after=$(status_field session)
        [[ "${session_after}" == "${session}" ]] && pass "the same session was resumed" "${session_after}" \
                                               || fail "the same session was resumed" "${session} -> ${session_after}"

        sleep $(( INTERVAL + 6 ))
        frames_after=$(status_field frames); frames_after="${frames_after:-0}"
        delta=$(( frames_after - frames_before ))
        (( delta >= 1 )) && pass "frames continued after the reboot" "${frames_before} -> ${frames_after}" \
                         || fail "frames continued after the reboot" "${frames_before} -> ${frames_after}"

        # the app must not replay the shots missed while it was off
        missed=$(( downtime / INTERVAL ))
        if (( delta <= 3 )); then
            pass "no catch-up burst of missed frames" \
                 "~${missed} shots missed, ${delta} taken"
        else
            fail "no catch-up burst of missed frames" \
                 "~${missed} shots missed, ${delta} taken"
        fi

        gaps_after=$(count_remote "ls -1 ${REMOTE_DIR}/timelapse/${session}/ 2>/dev/null | grep -o 'frame_[0-9]*' | sort -u | sed 's/frame_//' | sort -n | awk 'NR>1 && \$1!=prev+1 {c++} {prev=\$1} END {print c+0}'")
        gaps_after=${gaps_after:-0}
        [[ "${gaps_after}" == "0" ]] && pass "numbering survived the reboot" \
                                     || fail "numbering survived the reboot" "${gaps_after} gap(s)"
    fi
else
    skip "resume across a reboot" "run again with --reboot to test it"
fi

# ---------------------------------------------------------------- focus helper
say "==> Checking the focus stream"
api "/timelapse/stop" -X POST >/dev/null 2>&1 || true
sleep 2
focus_out=$(mktemp)
curl -s --max-time 8 -o "${focus_out}" "${BASE}/focus" >/dev/null 2>&1 || true
focus_bytes=$(wc -c < "${focus_out}" 2>/dev/null || echo 0)
focus_frames=$(grep -c -- '--frame' "${focus_out}" 2>/dev/null || echo 0)
rm -f "${focus_out}"
if (( focus_bytes > 2000 )) && (( focus_frames >= 1 )); then
    pass "the focus stream produces frames" "${focus_bytes} bytes, ${focus_frames} frame marker(s)"
else
    fail "the focus stream produces frames" "${focus_bytes} bytes, ${focus_frames} frame marker(s)"
fi

# ---------------------------------------------------------------- restore
if [[ "${KEEP_RUNNING}" == "0" ]]; then
    api "/timelapse/stop" -X POST >/dev/null 2>&1 && say "==> Test timelapse stopped"
else
    say "==> Leaving the timelapse running"
fi

# focusing leaves the ISP neutral, so bring the saved settings back
remote "sudo -n systemctl restart '${SERVICE_NAME}'" >/dev/null 2>&1 || true
code=$(curl -s -o /dev/null -w '%{http_code}' --retry 8 --retry-delay 2 --retry-connrefused \
       --max-time 25 "${BASE}/" || true)
[[ "${code}" == "200" ]] && pass "the service recovered after the restart" \
                         || fail "the service recovered after the restart" "HTTP ${code}"
say "    did the restart resume the run: $(status_field active 2>/dev/null)"

# ---------------------------------------------------------------- summary
say ""
python3 - "${RESULTS[@]}" <<'PY'
import sys
rows = [r.split("|", 2) for r in sys.argv[1:]]
width = max(len(r[1]) for r in rows)
failed = 0
for status, name, extra in rows:
    print(f"{status:<4} {name:<{width}}  {extra}")
    failed += status == "FAIL"
print(f"\n{len(rows) - failed}/{len(rows)} checks passed")
sys.exit(1 if failed else 0)
PY
