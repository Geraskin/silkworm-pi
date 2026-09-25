#!/usr/bin/env bash
#
# Stabilize a finished timelapse session into an MP4.
#
# The frames the camera writes are the record: this script never changes them.
# It reads `frame_*.jpg` in a session folder, measures how the whole picture
# moved from frame to frame, and reverses that movement so a box that was
# nudged - by a child, by a cat, by the wind - does not make the run swing.
#
# What it cannot do: undo what the plant itself did. Leaves that open, curl and
# turn towards the light are the subject, and they stay exactly as they were
# shot. Only the drift of the whole frame is compensated, which costs a small
# crop around the edges: the source frames have to cover the frame after it has
# been shifted, so the result is zoomed slightly.
#
# Usage:
#   bash scripts/stabilize-timelapse.sh <session-folder> [options]
#
#   --fps N            frame rate of the finished video (default 25)
#   --height N         output height in pixels (default 1080)
#   --smoothing N      how many frames the camera path is smoothed over
#                      (default 12; higher is calmer, lower follows faster)
#   --zoom P           how far to zoom in to hide the shifted edges, percent
#                      (default 2; raise it if black or warped edges show)
#   --crf N            H.264 quality, lower is better (default 18)
#   --no-stabilize     only assemble the video, do not correct movement
#   --keep-analysis    keep the measured camera path next to the video
#   -h, --help         this text
#
# Output: <session-folder>/timelapse-<height>p[-stabilized].mp4
#
# Exit code is 0 only when the video was written and is playable.

set -uo pipefail

PROG="$(basename "$0")"

die() {
    printf '%s: %s\n' "${PROG}" "$*" >&2
    exit 1
}

usage() {
    sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

SESSION=""
FPS=25
HEIGHT=1080
SMOOTHING=12
ZOOM=2
CRF=18
STABILIZE=1
KEEP_ANALYSIS=0

# Options that take a value are matched with a separate `needs_value` step: a
# bare `--fps` at the end of the line used to leave the variable empty, which the
# numeric check then reported as "expected a number" - an error about the wrong
# thing, and one that only appears when the option is last.
while [[ $# -gt 0 ]]; do
    case "$1" in
        --fps|--height|--smoothing|--zoom|--crf)
            option="$1"
            [[ $# -ge 2 && -n "${2:-}" ]] || die "${option} needs a value"
            case "${option}" in
                --fps)       FPS="$2" ;;
                --height)    HEIGHT="$2" ;;
                --smoothing) SMOOTHING="$2" ;;
                --zoom)      ZOOM="$2" ;;
                --crf)       CRF="$2" ;;
            esac
            shift 2 ;;
        --no-stabilize)   STABILIZE=0; shift ;;
        --keep-analysis)  KEEP_ANALYSIS=1; shift ;;
        -h|--help)        usage; exit 0 ;;
        -*)               die "unknown option: $1 (try --help)" ;;
        *)                [[ -z "${SESSION}" ]] || die "only one session folder at a time"
                          SESSION="$1"; shift ;;
    esac
done

[[ -n "${SESSION}" ]] || { usage >&2; exit 2; }
[[ -d "${SESSION}" ]] || die "no such folder: ${SESSION}"

command -v ffmpeg >/dev/null 2>&1 || die "ffmpeg is not installed"
command -v ffprobe >/dev/null 2>&1 || die "ffprobe is not installed"

for value in "${FPS}" "${HEIGHT}" "${SMOOTHING}" "${ZOOM}" "${CRF}"; do
    [[ "${value}" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "expected a number, got: ${value}"
done
[[ "${FPS}" != "0" ]] || die "--fps must be more than zero"
[[ "${HEIGHT}" != "0" ]] || die "--height must be more than zero"

# The listing is searched with `grep -c` on a captured string rather than a
# pipeline ending in `grep -q`: -q exits at the first match, ffmpeg is killed by
# SIGPIPE, and with pipefail the pipeline then reports failure even though the
# filter is right there. The check would have failed on every machine that has it.
if [[ -n "${SESSION}" && -d "${SESSION}" && "${STABILIZE}" -eq 1 ]]; then
    FILTER_LIST="$(ffmpeg -hide_banner -filters 2>/dev/null || true)"
    if ! grep -q ' vidstabdetect ' <<<"${FILTER_LIST}"; then
        die "this ffmpeg has no vidstabdetect filter; rerun with --no-stabilize"
    fi
fi

# Frames are named by the camera, so the sequence is built from a pattern rather
# than from a glob: a missing frame number then stops the encode instead of
# quietly shortening the run by one frame.
FIRST_IMAGE="$(find "${SESSION}" -maxdepth 1 -type f -name 'frame_*.jpg' | sort | head -1)"
[[ -n "${FIRST_IMAGE}" ]] || die "no frame_*.jpg in ${SESSION}"

FIRST_NUMBER="$(basename "${FIRST_IMAGE}" .jpg | sed 's/^frame_//')"
[[ "${FIRST_NUMBER}" =~ ^[0-9]{6}$ ]] || die "unexpected frame name: $(basename "${FIRST_IMAGE}")"

FRAME_COUNT="$(find "${SESSION}" -maxdepth 1 -type f -name 'frame_*.jpg' | wc -l | tr -d ' ')"
LAST_NUMBER="$(find "${SESSION}" -maxdepth 1 -type f -name 'frame_*.jpg' | sort | tail -1 | xargs -r basename | sed 's/^frame_//;s/\.jpg$//')"
EXPECTED=$(( 10#${LAST_NUMBER} - 10#${FIRST_NUMBER} + 1 ))
[[ "${FRAME_COUNT}" -eq "${EXPECTED}" ]] || \
    die "the frames have gaps: ${FRAME_COUNT} files span frame ${FIRST_NUMBER} to ${LAST_NUMBER}"

SUFFIX=""
[[ "${STABILIZE}" -eq 1 ]] && SUFFIX="-stabilized"
OUTPUT="${SESSION}/timelapse-${HEIGHT}p${SUFFIX}.mp4"
TRANSFORMS="${SESSION}/.camera-motion.trf"

INPUT_ARGS=(-framerate "${FPS}" -start_number "$(( 10#${FIRST_NUMBER} ))"
            -i "${SESSION}/frame_%06d.jpg")

# format=yuv420p is not cosmetic: without it the file plays in some players and
# shows nothing in others, which looks exactly like a broken download.
FORMAT_FILTER="scale=-2:${HEIGHT}:flags=lanczos,format=yuv420p"

if [[ "${STABILIZE}" -eq 1 ]]; then
    printf 'Measuring camera movement in %s (%s frames)...\n' "${SESSION}" "${FRAME_COUNT}"
    # mincontrast is lowered because a frame of seedlings has large dark areas
    # the default threshold discards, which would leave the motion unmetered.
    rm -f "${TRANSFORMS}"
    ffmpeg -hide_banner -loglevel error -y "${INPUT_ARGS[@]}" \
        -vf "vidstabdetect=result=${TRANSFORMS}:shakiness=5:accuracy=15:mincontrast=0.1" \
        -f null - || die "measuring the camera path failed"
    [[ -s "${TRANSFORMS}" ]] || die "no camera path was measured (${TRANSFORMS})"

    printf 'Rendering %s...\n' "${OUTPUT}"
    # The transform is applied before the scale, so the correction is measured
    # on the real pixels. zoom adds the margin the shift needs: the frames must
    # still cover the picture after the path has been reversed.
    ffmpeg -hide_banner -loglevel error -y "${INPUT_ARGS[@]}" \
        -vf "vidstabtransform=input=${TRANSFORMS}:smoothing=${SMOOTHING}:zoom=${ZOOM}:interpol=bicubic,${FORMAT_FILTER}" \
        -c:v libx264 -preset medium -crf "${CRF}" -movflags +faststart \
        "${OUTPUT}" || die "encoding failed"
else
    printf 'Rendering %s (%s frames, no stabilization)...\n' "${OUTPUT}" "${FRAME_COUNT}"
    ffmpeg -hide_banner -loglevel error -y "${INPUT_ARGS[@]}" \
        -vf "${FORMAT_FILTER}" \
        -c:v libx264 -preset medium -crf "${CRF}" -movflags +faststart \
        "${OUTPUT}" || die "encoding failed"
fi

[[ -s "${OUTPUT}" ]] || die "the encoder wrote nothing to ${OUTPUT}"

DURATION="$(ffprobe -v error -select_streams v:0 -show_entries stream=duration \
    -of default=noprint_wrappers=1:nokey=1 "${OUTPUT}" 2>/dev/null)"
[[ -n "${DURATION}" && "${DURATION}" != "N/A" ]] || \
    DURATION="$(ffprobe -v error -show_entries format=duration \
        -of default=noprint_wrappers=1:nokey=1 "${OUTPUT}" 2>/dev/null)"

if [[ "${KEEP_ANALYSIS}" -eq 0 ]]; then
    rm -f "${TRANSFORMS}"
else
    printf 'Camera path kept in %s\n' "${TRANSFORMS}"
fi

printf 'Done: %s\n' "${OUTPUT}"
printf 'Frames: %s at %s fps - %s s\n' "${FRAME_COUNT}" "${FPS}" "${DURATION:-?}"
