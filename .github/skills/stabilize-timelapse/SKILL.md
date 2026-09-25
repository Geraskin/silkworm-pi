---
name: stabilize-timelapse
description: 'Turn a finished timelapse session into an MP4, with the drift of the whole picture corrected. Use when: assemble timelapse into video, the timelapse is wobbly, the box moved during the run, camera shifted between frames, stabilize frames, make a video from frame_*.jpg, ffmpeg vidstab, the seedlings swing back and forth.'
argument-hint: '<session-folder> [--fps N] [--height N] [--zoom P]'
---

# Stabilize a timelapse session

Assembles `frame_*.jpg` from one session into an MP4, correcting the drift of the
whole picture so a run that was nudged does not swing.

## What this does and does not fix

The tool measures how the **whole frame** moved between shots and reverses it.
That covers the camera being knocked, the box being bumped, the whole rig
settling.

It does **not** touch what the plants themselves did. Seedlings opening, curling
and turning towards the light are the subject of the recording; they stay exactly
as they were shot. If they seem to move back and forth, that is the plant.

Correcting the frame costs a small crop: after the picture has been shifted it
still has to cover the frame, so the result is zoomed slightly (2% by default).

### A bright band down one side is not drift

A frame that is brighter on one side than the other, or that has a pink or blue
cast, is **not** something this tool corrects and not something the stabiliser
caused. On this rig it is uneven illumination of the box, and it is present in
every frame of a run, including the first.

It is worth knowing before blaming the video, because a gradient looks like a
defect. Measured on the first run: a lit frame under even light has a spread of
**1** level across the frame, a run frame has **49** — and the shape is identical
in frame 1 and frame 286, two days apart. A camera fault or a cable fault does not
stay that still, and it does not disappear when the light is even.

To tell the two apart quickly, measure the frame in eight vertical strips:

```bash
python3 - <<'PY'
import subprocess
p = "frame_000119.jpg"
for i in range(8):
    r = subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-i",p,
        "-vf",f"crop=iw/8:ih:{i}*iw/8:0,scale=1:1,format=rgb24",
        "-f","rawvideo","-"], capture_output=True)
    c = tuple(r.stdout[:3]); print(f"strip {i}: {c} luma={sum(c)//3}")
PY
```

A shape that is stable across the run and scales with the light is illumination,
and belongs to the box rather than to the tool. See
[docs/flat-field-plan.md](../../../docs/flat-field-plan.md) for removing it.

## Procedure

### 1. Get the frames

Frames live on the Pi in `~/camweb/timelapse/<session>/`, and sessions are
usually archived to the NAS under `<YYYY-MM-DD>/<session>/`. Copy the session
folder to the machine that will do the encoding — the Pi itself is busy shooting
and should not be asked to render video.

```bash
rsync -av "pi@<pi>:~/camweb/timelapse/tl-20260923-134701/" ./tl-20260923-134701/
```

### 2. Stabilize

```bash
bash scripts/stabilize-timelapse.sh ./tl-20260923-134701
```

Output: `./tl-20260923-134701/timelapse-1080p-stabilized.mp4`.

The frames are never modified. Two passes are run: the first only measures the
camera path, the second renders the video with that path reversed.

## Options

| Option | Default | Purpose |
|--------|---------|---------|
| `--fps N` | 25 | frame rate of the result; lower makes a longer, calmer video |
| `--height N` | 1080 | output height in pixels; width follows the aspect |
| `--smoothing N` | 12 | frames the camera path is smoothed over — higher is calmer, lower follows quick movement |
| `--zoom P` | 2 | percent zoomed in to hide the shifted edges; raise it if dark edges appear |
| `--crf N` | 18 | H.264 quality, lower is better |
| `--no-stabilize` | off | assemble the video only, with no correction |
| `--keep-analysis` | off | keep the measured camera path (`*.camera-motion.trf`) next to the video |

## Choosing the settings

- **Motion still visible in the result.** The movement is either faster than the
  smoothing, or larger than the zoom can hide. Lower `--smoothing` so the
  correction follows it more closely, and raise `--zoom` enough for the shift.
- **Dark or smeared edges.** The shift went beyond the margin. Raise `--zoom`.
- **The plants look too fast.** Lower `--fps`, or re-shoot with a shorter
  interval.
- **A very long run.** Encoding an 8 MP frame per frame is the slow part; set
  `--height 720` while experimenting, then render the final video at 1080p.

## Checks worth running in the session folder

```bash
# how many frames, and the first and last
find <session> -name 'frame_*.jpg' | wc -l
ls <session>/frame_*.jpg | head -1; ls <session>/frame_*.jpg | tail -1
```

The script refuses to run when the numbering has a hole in it. That is
deliberate: an encoder reading `frame_%06d.jpg` skips a missing number and
produces a slightly shorter video, which looks like a shorter run rather than a
broken one. Frame numbers are the run's record, so a gap is an error.

## Limits to know about

- `vidstab` is part of FFmpeg; if the build lacks it the script says so and
  `--no-stabilize` still assembles the video.
- Timelapse frames are shot at intervals of minutes, so between two frames a real
  plant can move further than the stabiliser can reasonably attribute to the
  camera. When in doubt, check the still frames before blaming the tool.
- Stabilisation is a viewing aid, not a restoration: the corrected video is a
  derived file and the original frames remain the record of the run.
