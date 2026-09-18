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
- Timelapse frames are written locally first and pushed to a NAS afterwards (`nas_flush()`), so a sleeping or unreachable NAS cannot lose a frame. The card is a **cache, not a queue**: local copies are kept, and `nas_prune()` only starts removing them once free space falls below `nas_min_free_percent`, oldest and already-confirmed-on-the-NAS first. A local file is removed only when its NAS copy exists with the same size, so rotation can never delete the only copy; frames not yet uploaded are never touched, which is what makes an unreachable NAS safe. Each file is copied to `<name>.part` and renamed only after the size matches, and `state.json` never leaves the Pi. **The app never knows the NAS address** — mounting the share is the Pi's job (`/etc/fstab`), the app only gets a mount path from `settings.json`, so no address or credentials can leak into this repository.
- Focus mode forces the full-sensor resolution and streams 1:1 centre crops (`/focus`) with denoise off and sharpness neutral, so focus is judged on real pixels, not on ISP sharpening.
- The Pi's hardware H.264 encoder cannot encode the 8 MP mode: recording switches to a supported size and restores the still resolution afterwards.
- Preview and video cannot run at the same time (both use the V4L2 encoder); the preview resumes once recording stops.
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
check the response on `:8080`. Full commands are in the `deploy-pi` skill.

## Conventions

- **Language: all project documentation is written in English** — `AGENTS.md`, `SKILL.md`, code comments, and user-facing script messages. Follow this when creating or updating any docs.
- Production code is `app_v2.py` only. Do not deploy `app.py` to the Pi.
- Camera settings live on the Pi in `~/camweb/settings.json` — never commit or overwrite them during deploy.
- After code changes, offer deployment via the `deploy-pi` skill.
- Commands on the Pi that require root (systemctl) are run via `sudo` — a passwordless sudoers rule is configured for `systemctl restart/status/enable camweb`.
