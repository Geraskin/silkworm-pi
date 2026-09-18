# Silkworm Pi

A **timelapse camera** built on a Raspberry Pi and driven entirely from a browser.

Point it at a plant, a building site or the sky, choose an interval, and walk away.
The Pi keeps shooting full-resolution frames one after another — and if the power
fails or the board reboots, it **picks the run back up instead of starting over**.

Everything else in the app exists to make that job easier: a live preview, a 1:1
focus helper, camera controls, and a lamp you can drive to keep the light steady.

---

## Timelapse

The point of the project.

- **Any interval**, in seconds.
- **Full sensor resolution** — 3280×2464 on an IMX219 — regardless of the preview
  resolution. Every frame is worth keeping.
- **Frames stay individual files.** The app never assembles a video, so it never
  locks you into a frame rate or a codec. You decide afterwards:

  ```bash
  ffmpeg -framerate 25 -pattern_type glob -i 'frame_*.jpg' \
         -c:v libx264 -pix_fmt yuv420p timelapse.mp4
  ```

- **Resumes after a reboot or a power cut.** Progress lives in `state.json`
  (`session`, `interval_s`, `frames`, `last_shot_at`, `next_shot_at`). On start-up
  the app reads it and continues the same session with the same frame numbering.
  The file is written atomically (`tmp` + `os.replace`), so even losing power
  mid-write cannot leave it half-finished.
- **No catch-up.** If the Pi was off for six hours, the next frame is simply taken
  at the next slot — the app does not fire off a burst of missed shots.
- **RAW on request.** Tick *Also save RAW* and every frame is written as `.dng`
  next to its `.jpg`.
- **Disk guard.** If free space drops below 500 MB the run stops cleanly and
  records the reason in `state.json`, instead of filling the card.

Output layout:

```
~/camweb/timelapse/
├── state.json                     # progress - survives a reboot
└── tl-20260918-143000/            # one folder per session
    ├── frame_000001.jpg
    ├── frame_000001.dng           # when RAW is enabled
    ├── frame_000002.jpg
    └── ...
```

The *Timelapse* card shows the session name, the frame count and the countdown to
the next shot.

## Focus helper

Focusing a fixed-focus module on a subject is guesswork unless you can see real
pixels. The focus helper streams **1:1 centre crops** taken from full-resolution
frames — one sensor pixel per screen pixel, no scaling and no interpolation.

While focusing, the ISP is forced neutral (`denoise` off, `sharpness` neutral) so
it cannot fake detail that the lens is not resolving.

The crop size is selectable: `320x240` (strongest magnification), `640x480` (the
default) and `1280x960` (more of the frame). Magnifying beyond 1:1 adds nothing but
interpolation, so the app does not offer it.

## Live preview and stills

- Hardware-accelerated **MJPEG preview** with a configurable bitrate. It keeps
  running while you take photos.
- **Stills** as JPEG, with quality control, plus an optional **RAW/DNG**.
- **Rotation** by 0/90/180/270. The Pi's ISP can only flip, so 180° is done in
  hardware and 90°/270° in software for stills (CSS rotates the preview).
- Settings are applied to the live preview as soon as you change them.

## Camera controls

Resolution presets, brightness, contrast, saturation, sharpness and EV; auto white
balance, metering and exposure modes; denoise mode; and full **manual exposure**
(shutter time picked as familiar fractions such as 1/100 s, and analogue gain).

## Lamp

A backlight lamp on a TB6612 driver, with brightness control. The lamp is wired as
`AIN1=GPIO23`, `AIN2=GPIO24`, `STBY=GPIO25`, `PWM=GPIO18` and dimmed over the Pi's
**hardware PWM** channel, because `gpiozero`'s software PWM cannot render short
pulses: at high frequency the duty cycle drifts and the lamp visibly flickers. When
the hardware channel is unavailable the app falls back to software PWM and says so
in the UI.

## Board status and power

Temperature, throttling flags, uptime and disk usage (`vcgencmd`), plus **reboot**
and **shutdown** buttons in the top bar.

---

## Hardware

| Part | Notes |
|------|-------|
| Raspberry Pi 3 Model B or newer | developed on a Pi 3 B rev 1.2 |
| Camera Module (IMX219) | 3280×2464, 10-bit RGGB |
| TB6612 motor driver | drives the lamp |
| Backlight lamp | on the TB6612 output |

## Install

1. **Raspberry Pi OS** (Bookworm or newer) with the camera interface enabled.
2. **Dependencies:**

   | Package | Used for |
   |---------|----------|
   | `python3-picamera2` / `libcamera` | the camera itself (ships with Raspberry Pi OS) |
   | `python3-pil` (Pillow) | JPEG encoding and 90°/270° rotation |
   | `pidng` | writing DNG files |
   | `python3-flask` | the web interface |
   | `python3-gpiozero` + `rpi-lgpio` | the lamp (optional — the app runs without them) |

3. **Copy the app to the Pi and run it:**

   ```bash
   mkdir -p ~/camweb
   cp app_v2.py camera.py ~/camweb/
   cd ~/camweb && python3 app_v2.py
   ```

4. Open `http://<pi-address>:8080/` in a browser.

### Run it as a service

The app is meant to start at boot, so a timelapse survives a power cut without
anyone logging in. A ready unit is in
[`camweb.service`](./.github/skills/deploy-pi/assets/camweb.service) — set
`User=` and the paths to your account, then:

```bash
sudo cp camweb.service /etc/systemd/system/
sudo systemctl enable --now camweb
```

`Restart=always` in the unit is what brings the app (and the timelapse) back after
a reboot.

### Lamp: hardware PWM

Enable the PWM channel in `/boot/firmware/config.txt`:

```
dtoverlay=pwm,pin=18,func=2
dtparam=audio=off
```

`audio=off` is required because PWM0 is shared with the analogue audio output.
Export the channel at boot and let the app write to it: a one-shot unit does the
export and grants group `gpio` write access, and the app's user belongs to that
group. Without it the app falls back to software PWM, which is noticeably worse.

---

## App data

Everything the app produces lives in `~/camweb/`. None of it is in the repository:

| Path | Contents |
|------|----------|
| `latest.jpg` | the last still |
| `latest.dng` | the last RAW capture |
| `camweb.h264` | the last video recording |
| `timelapse/` | timelapse sessions and `state.json` |
| `settings.json` | saved camera and lamp settings |

## Development

The repository ships a dev container (`.devcontainer/`) with Python 3.12, Flask and
`gpiozero`, plus `openssh-client` and `rsync`, and forwards your host SSH keys so it
can deploy.

The app imports cleanly off the Pi: `picamera2` and `vcgencmd` are absent, so
`camera.AVAILABLE` is `False` and the lamp is disabled. That is enough for UI work —
but taking a photo needs the real hardware.

### Deploy

Deployment is handled by the `deploy-pi` skill (`.github/skills/deploy-pi/`): it
verifies SSH, uploads the app files with `rsync`, restarts the service and checks
the response.

Your Pi's address and key path are **not** in the repository. They live in the
gitignored `.github/skills/deploy-pi/deploy.env`:

```bash
cp .github/skills/deploy-pi/deploy.env.example .github/skills/deploy-pi/deploy.env
$EDITOR .github/skills/deploy-pi/deploy.env
bash .github/skills/deploy-pi/scripts/deploy.sh
```

## Notes and limitations

- **Rotation:** the Pi ISP flips but cannot rotate. 90°/270° are done in software
  for stills and with CSS in the browser for the preview.
- **Recording and preview are mutually exclusive:** both use the V4L2 encoder, so
  the preview pauses while a video is being recorded and resumes afterwards.
- **8 MP cannot be encoded in hardware:** recording switches to a supported size and
  restores the still resolution when it stops.
- **One camera instance serves everything.** Flask is multi-threaded, so a lock
  serialises access to the camera.
- **Colour:** libcamera's `RGB888` is V4L2 `RGB24`, which is BGR in memory. The still
  path swaps the red and blue channels before handing the array to PIL.

## Repository layout

| File | Purpose |
|------|---------|
| `app_v2.py` | production application — Flask UI |
| `camera.py` | picamera2/libcamera backend |
| `app.py` | the original basic version, kept for reference |
| `AGENTS.md` | project notes and conventions |
| `.github/skills/deploy-pi/` | deployment skill and script |
