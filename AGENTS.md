# AGENTS.md — Silkworm Pi

## What this project is

Silkworm Pi is a web application for a Raspberry Pi with a camera (Camera Module / IMX219). From a browser you can:
- watch a live MJPEG preview and take photos (JPEG, optionally + RAW/DNG) via picamera2 (libcamera);
- record H.264 video;
- run a resumable timelapse (frames as individual files) and use a 1:1 focus helper;
- view board status (`vcgencmd measure_temp`, `get_throttled`, uptime, disk);
- configure the camera (resolution, brightness, contrast, exposure, denoise, etc.);
- control a backlight lamp via the TB6612 driver (`gpiozero`).

The app runs on the Pi itself: Flask on `0.0.0.0:8080`.

## Repository structure

- `app_v2.py` — **main (production) version**: Flask UI, extended camera settings, manual exposure, lamp control, power actions, settings saved to `~/camweb/settings.json`.
- `camera.py` — picamera2 backend: one camera process serves live preview (MJPEG), stills (JPEG + optional DNG) and video (H.264).
- `app.py` — old basic version (no lamp, no extended settings). Do not touch unless explicitly needed.
- `tests/` — host-side checks that need neither camera nor Pi (see below).
- `README.md` — what the project is (a Raspberry Pi timelapse camera first) and how to install it.
- `.devcontainer/` — local dev environment (Python 3.12, Flask, `gpiozero`, plus `openssh-client` + `rsync` for deployment). Machine-specific and **gitignored**: it is not part of the published repository.

## Technologies

Python 3.12 · Flask · gpiozero · rpicam-still (libcamera) · vcgencmd (Raspberry Pi OS)

## App data

On the Pi the app keeps working data in `~/camweb/`:
- `latest.jpg` — the last capture;
- `latest.dng` — the last RAW capture (when "Also save RAW" is on);
- `camweb.h264` — the last video recording;
- `timelapse/<session>/` — timelapse frames (`frame_000001.jpg`, plus `.dng` with RAW on) and `timelapse/state.json`, which holds the schedule and progress;
- `settings.json` — saved camera and lamp settings.

These files live only on the Pi and are not committed to the repo.

## How the code works

- `picamera2`/`libcamera` and `vcgencmd` exist only on Raspberry Pi OS. A photo cannot be taken on a dev machine.
- One `Picamera2` instance serves preview, stills and video (see `camera.py`); a lock serialises access because Flask is multi-threaded.
- The `gpiozero` import is wrapped in `try/except` — off the Pi the app runs without lamp control (`GPIO_AVAILABLE = False`).
- Stills are written to a temp file and atomically replaced (`os.replace`).
- libcamera's `RGB888` is V4L2 `RGB24`, i.e. **BGR in memory** — the still capture swaps the R/B channels before handing the array to PIL, otherwise red and blue come out swapped.
- The timelapse runs in a background thread that reads `timelapse/state.json` on start-up, so an interrupted run **resumes after a reboot or power cut**; the state is written atomically (`tmp` + `os.replace`) and the next shot time is advanced around every frame. Frames are stored as individual JPEGs — the app does **not** build a video.
- Timelapse frames are written locally first and pushed to a NAS afterwards (`nas_flush()`), so a sleeping or unreachable NAS cannot lose a frame. The card is a **cache, not a queue**: local copies are kept, and `nas_prune()` only starts removing them once free space falls below `nas_min_free_percent`, oldest and already-confirmed-on-the-NAS first. A local file is removed only when its NAS copy exists with the same size, so rotation can never delete the only copy; frames not yet uploaded are never touched, which is what makes an unreachable NAS safe. Each file is copied to `<name>.part` and renamed only after the size matches, and a captured frame is itself written to a temporary name and renamed into place, so a sweep can never publish a half-written image. `state.json` never leaves the Pi. **`nas_check()` requires the folder to be a real mount point** (`os.path.ismount`) and not inside the cache: if the share is not mounted the path is just a directory on the card, and the rotation would then delete the "original" because the copy looks safe. Sessions that are already fully archived are tracked in `_nas_mirrored` so a sweep does not re-list thousands of files. On the NAS a session lands in `<nas_dir>/<YYYY-MM-DD>/<session>/`, grouped by day; the date comes from the session name, so a frame always goes to the same place. Each session carries a `session.json` next to the frames describing how and when it was shot (start/stop, interval, resolution, rotation, exposure, denoise, white balance, lamp), rewritten as the run progresses and re-uploaded when its content changes - compared by SHA-256, not by mtime, because timestamp granularity is coarse (tmpfs ticks in ms, an SD card in whole seconds) and a same-length rewrite inside one tick would otherwise look unchanged. **The app never knows the NAS address** — mounting the share is the Pi's job (`/etc/fstab`), the app only gets a mount path from `settings.json`, so no address or credentials can leak into this repository.
- Focus mode forces the full-sensor resolution and streams 1:1 centre crops (`/focus`) with denoise off and sharpness neutral, so focus is judged on real pixels, not on ISP sharpening. Each of those frames is also measured for sharpness (`camera.sharpness_score`, mean neighbour gradient over mean brightness, smoothed first so sensor noise does not read as detail) and the number is shown next to the picture with a peak-hold and a history strip: `GET /focus/score` only reads back what the stream measured, `POST /focus/score` clears the peak. The score is never computed on request - polling the endpoint must not touch the camera, which in this app is the difference between a live preview and a dead one. The values are relative (same scene, moment to moment), and the smoothing is a 3x3 binomial because a box of the same width nulls out detail at its own period (three pixels), which would make the meter dip on a perfectly sharp subject. The brightness the gradient is divided by has a floor (`camera.FOCUS_SCORE_DARK`): without it a covered lens scored 218 (mean brightness 1.4/255), the best number the meter had ever shown. Only the centre of the crop is measured (`camera.score_window`, at most 640x480 pixels): a 1280x960 crop costs four times as much to measure as the frame itself takes to capture, which would halve the focus frame rate for no extra information.
- The Pi's hardware H.264 encoder cannot encode the 8 MP mode: recording switches to a supported size and restores the still resolution afterwards.
- Preview and video cannot run at the same time (both use the V4L2 encoder); the preview resumes once recording stops.
- **The preview encoder and still capture must not be mixed carelessly.** A standalone `stop_recording()` leaves the camera in a state where the next capture blocks **for ever and silently** — no return, no exception, so the app looks healthy while quietly producing no frames. The rules that come out of that: the encoder is only stopped inside a full camera teardown; switching focus mode or the preview off restarts the camera rather than stopping the encoder; captures take a still request (`capture_request().make_array()`) instead of `capture_array()`, which is tied to the video stream buffers; and `Camera._watchdog` ends the process if a capture stays stuck for `WATCHDOG_S` (45 s) so systemd's `Restart=always` brings the service back instead of leaving a dead camera behind.
- The lamp uses **hardware PWM** (PWM0 on GPIO 18) through `/sys/class/pwm`, enabled by `dtoverlay=pwm,pin=18,func=2` + `dtparam=audio=off` in `/boot/firmware/config.txt`. A one-shot unit (`lamp-pwm.service`) exports the channel at boot and grants group `gpio` write access; the app's user is in that group. If the channel is missing the app falls back to gpiozero's software PWM, which is much less accurate (duty drifts at high frequency).

## Skills (superpowers)

Skills are bundles of instructions and scripts that the agent loads **on demand**. They live in `.github/skills/<name>/SKILL.md`. The agent discovers a skill by its `description` field, which must contain trigger keywords.

Usage rules:
1. If a task matches a skill's description, first read its `SKILL.md` fully and follow the procedure.
2. Resource paths inside a skill are relative to the skill, using `./`.
3. Do not hard-code deploy commands in code — use the skill.

Available skills in this repo:

| Skill | When to use |
|-------|-------------|
| `deploy-pi` | "deploy", "update on the Pi", "upload over SSH", "restart the server on the Raspberry Pi" |

## Deployment to the Raspberry Pi

The connection details are **not** in the repository - they live in the gitignored
`.github/skills/deploy-pi/deploy.env`. Read that file before deploying; if it is
missing, copy `deploy.env.example` and fill it in.

- The Pi's address is DHCP-assigned and can change. `*.local` does **not** resolve
  inside the dev container, because mDNS does not cross the Docker bridge.
- Pi folder: `~/camweb` - production file: `app_v2.py`
- Restart: the `camweb` systemd service is installed and enabled; sudo is
  passwordless for `systemctl restart|status|enable camweb`.

Order: verify SSH → upload files with `rsync` → `sudo systemctl restart camweb` →
check the response on `:8080`. Full commands are in the `deploy-pi` skill, which
also ships `smoke-test.sh` — it starts a short timelapse, checks the frames, the
NAS cache and the focus stream on the real hardware, and with `--reboot` verifies
that an interrupted run resumes.

## Tests

`tests/` holds checks that run on any machine — no camera, no Pi, no privileges:
`python3 tests/test_nas_export.py`, and the same for `test_nas_queue.py` and
`test_nas_sync.py`. Each is a plain script that prints `PASS`/`FAIL` and exits
non-zero, so they can be run in a loop.

Deciding whether a folder is the share or the card needs a real mount, and a
container cannot mount anything. `/dev/shm` is a tmpfs that already *is* a mount
point, so it stands in for the NAS, while an ordinary folder stands in for a path
that exists but is not a share.

`test_nas_sync.py` runs the background sweep off a fake clock. The failures worth
catching there are about *when* a pass happens rather than what it does, and a
suite that only ever calls the sweep with `force=True` never sees them — which is
exactly how a sweep that ran once per boot and then never again went unnoticed.

## Conventions

- **Language: all project documentation is written in English** — `AGENTS.md`, `SKILL.md`, code comments, and user-facing script messages. Follow this when creating or updating any docs.
- Production code is `app_v2.py` only. Do not deploy `app.py` to the Pi.
- Camera settings live on the Pi in `~/camweb/settings.json` — never commit or overwrite them during deploy.
- After code changes, offer deployment via the `deploy-pi` skill.
- Commands on the Pi that require root (systemctl) are run via `sudo` — a passwordless sudoers rule is configured for `systemctl restart/status/enable camweb`.
