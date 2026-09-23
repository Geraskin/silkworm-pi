from flask import Flask, Response, jsonify, render_template_string, request, send_file

import camera as cam
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

app = Flask(__name__)

BASE_DIR = Path.home() / "camweb"
IMAGE_PATH = BASE_DIR / "latest.jpg"
TMP_IMAGE_PATH = BASE_DIR / ".latest.tmp.jpg"
RAW_PATH = BASE_DIR / "latest.dng"
VIDEO_PATH = BASE_DIR / "camweb.h264"
SETTINGS_PATH = BASE_DIR / "settings.json"
BASE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SETTINGS = {
    "resolution": "1640x1232",
    "preview_enabled": True,
    "preview_bitrate": 15.0,
    "focus_mode": False,
    "focus_crop": "640x480",
    "quality": 93,
    "brightness": 0.0,
    "contrast": 1.0,
    "saturation": 1.0,
    "sharpness": 1.0,
    "ev": 0.0,
    "rotation": 0,
    "hflip": False,
    "vflip": False,
    "awb": "auto",
    "metering": "centre",
    "exposure": "normal",
    "denoise": "fast",
    "manual_exposure": False,
    "shutter": 10000,
    "gain": 1.0,
    "save_raw": False,
    "nas_enabled": False,
    "nas_dir": "",
    "nas_min_free_percent": 10,
    "light_on": False,
    "light_brightness": 0.5,
    "light_freq": 1000,
}

RESOLUTIONS = {
    "3280x2464": ("3280", "2464", "Full sensor · 8 MP"),
    "1640x1232": ("1640", "1232", "Focus / fast · 2 MP"),
    "1920x1080": ("1920", "1080", "Full HD · 16:9 crop"),
    "640x480": ("640", "480", "Preview · fast"),
}

# Live preview stream size (the picamera2 "lores" stream), 4:3 like the sensor.
PREVIEW_SIZE = (640, 480)

AWB_MODES = cam.AWB_MODES
METERING_MODES = cam.METERING_MODES
EXPOSURE_MODES = cam.EXPOSURE_MODES
DENOISE_MODES = cam.DENOISE_MODES
SHUTTER_SPEEDS = cam.SHUTTER_SPEEDS

capture_lock = threading.Lock()

# Single picamera2 instance: live preview + stills + video (see camera.py).
camera = cam.Camera(BASE_DIR, preview_size=(640, 480), fps=15)


def resolution_size(name):
    item = RESOLUTIONS.get(name) or RESOLUTIONS["1640x1232"]
    return int(item[0]), int(item[1])


FULL_SENSOR = (3280, 2464)

# 1:1 centre crops offered by the focus helper (no scaling is applied).
FOCUS_CROPS = ["320x240", "640x480", "1280x960"]


def camera_size():
    """Main-stream size: the full sensor while focusing or running a timelapse."""
    if SETTINGS.get("focus_mode") or timelapse_active():
        return FULL_SENSOR
    return resolution_size(SETTINGS["resolution"])


def orientation_flags():
    """Hardware flip flags. Rotation 180 equals both flips; 90/270 are done in
    software because the Pi ISP cannot rotate (only flip)."""
    rot = int(SETTINGS.get("rotation", 0)) % 360
    return {
        "hflip": bool(SETTINGS.get("hflip", False)) ^ (rot == 180),
        "vflip": bool(SETTINGS.get("vflip", False)) ^ (rot == 180),
    }


def still_output_size():
    """Expected still size: 90/270 rotations swap width and height."""
    w, h = camera_size()
    if int(SETTINGS.get("rotation", 0)) in (90, 270):
        return h, w
    return w, h


def preview_size():
    return PREVIEW_SIZE


def effective_controls():
    """The controls the camera should have right now.

    The focus helper needs the ISP out of the way, and it has to stay that way:
    applying the neutral settings only once when the stream starts would let any
    settings POST in the meantime quietly switch denoise and sharpening back on,
    and the focus view would stop showing real pixels.
    """
    s = dict(SETTINGS)
    if s.get("focus_mode"):
        s["denoise"] = "off"
        s["sharpness"] = 1.0
    return s


def camera_start():
    """Start the camera once and push the saved controls to it."""
    if not cam.AVAILABLE:
        return
    camera.start(
        camera_size(),
        preview_size=preview_size(),
        **orientation_flags(),
    )
    camera.apply_controls(effective_controls())


def load_settings():
    s = DEFAULT_SETTINGS.copy()
    try:
        if SETTINGS_PATH.exists():
            saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            for k in s:
                if k in saved:
                    s[k] = saved[k]
    except Exception:
        pass
    return s


SETTINGS = load_settings()


def save_settings():
    SETTINGS_PATH.write_text(
        json.dumps(SETTINGS, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


class SysfsPWM:
    """Hardware PWM through /sys/class/pwm (needs `dtoverlay=pwm,pin=18,func=2`).

    Gives exact timing at any frequency, unlike gpiozero's software PWM. Raises
    if the channel is not exported, so the caller can fall back.
    """

    def __init__(self, channel=0, chip="/sys/class/pwm/pwmchip0", frequency=1000):
        self.base = Path(chip) / f"pwm{channel}"
        if not self.base.is_dir():
            raise OSError(
                f"{self.base} is missing (is the pwm overlay enabled?)")
        self._frequency = int(frequency)
        self._value = 0.0
        self._write("duty_cycle", 0)
        self._write("period", self._period_ns())
        self._write("enable", 1)

    def _period_ns(self):
        return max(1000, int(1_000_000_000 / self._frequency))

    def _write(self, name, value):
        (self.base / name).write_text(str(int(value)), encoding="ascii")

    def _apply(self):
        period = self._period_ns()
        duty = int(period * self._value)
        self._write("duty_cycle", min(duty, period))
        self._write("enable", 1 if duty > 0 else 0)

    @property
    def frequency(self):
        return self._frequency

    @frequency.setter
    def frequency(self, hz):
        self._frequency = max(1, min(20000, int(hz)))
        try:
            self._write("duty_cycle", 0)
            self._write("period", self._period_ns())
            self._apply()
        except Exception:
            pass

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, v):
        self._value = max(0.0, min(1.0, float(v)))
        self._apply()

    def off(self):
        self.value = 0.0


try:
    from gpiozero import DigitalOutputDevice, PWMOutputDevice

    LIGHT_AIN1 = DigitalOutputDevice(23)
    LIGHT_AIN2 = DigitalOutputDevice(24)
    LIGHT_STBY = DigitalOutputDevice(25)
    try:
        LIGHT_PWM = SysfsPWM(frequency=1000)  # hardware PWM0 on GPIO18
        LIGHT_PWM_HW = True
    except Exception:
        LIGHT_PWM = PWMOutputDevice(
            18, frequency=1000)  # software PWM fallback
        LIGHT_PWM_HW = False
    GPIO_AVAILABLE = True
except Exception:
    LIGHT_AIN1 = LIGHT_AIN2 = LIGHT_STBY = LIGHT_PWM = None
    GPIO_AVAILABLE = False
    LIGHT_PWM_HW = False

# Last applied lamp state, so redundant PWM writes are skipped.
_LIGHT_STATE = {"on": None, "duty_pct": None, "freq": None}


def apply_light():
    """Apply the lamp state, skipping redundant PWM writes.

    Every PWMOutputDevice.value write re-arms the (software) PWM in lgpio, which
    can cause a brief visible flicker, so only write when the 1 % duty step or
    the on/off state actually changes.
    """
    if not GPIO_AVAILABLE:
        return
    on = bool(SETTINGS.get("light_on"))
    duty_pct = max(
        0, min(100, int(round(SETTINGS.get("light_brightness", 0.0) * 100))))
    try:
        freq = max(1, min(10000, int(SETTINGS.get("light_freq", 1000))))
    except Exception:
        freq = 1000
    changed_freq = freq != _LIGHT_STATE["freq"]
    if not changed_freq and on == _LIGHT_STATE["on"] and duty_pct == _LIGHT_STATE["duty_pct"]:
        return
    if changed_freq:
        try:
            LIGHT_PWM.frequency = freq
        except Exception:
            pass
        _LIGHT_STATE["freq"] = freq
    _LIGHT_STATE["on"] = on
    _LIGHT_STATE["duty_pct"] = duty_pct
    if on:
        LIGHT_STBY.on()
        LIGHT_AIN1.on()
        LIGHT_AIN2.off()
        LIGHT_PWM.value = duty_pct / 100
    else:
        LIGHT_PWM.off()
        LIGHT_AIN1.off()
        LIGHT_AIN2.off()
        LIGHT_STBY.off()


apply_light()


HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Silkworm Pi Camera</title>
<style>
:root {
    --bg:#f4f5f7; --card:#fff; --text:#202124; --muted:#687076;
    --line:#d8dde3; --primary:#2563eb; --good:#176b3a; --bad:#a12626;
}
* { box-sizing:border-box; }
body {
    margin:0; padding:18px 18px 70px; background:var(--bg); color:var(--text);
    font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
}
.statusbar {
    position:fixed; left:0; right:0; bottom:0; z-index:20;
    display:flex; flex-wrap:wrap; gap:6px 18px; align-items:center;
    padding:9px 16px; background:#202124; color:#e8eaed; font-size:12.5px;
}
.statusbar b { color:#9aa0a6; font-weight:600; margin-right:5px; }
h1 { margin:0 0 14px; font-size:25px; }
h2 { margin:0 0 12px; font-size:18px; }
.topbar { display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:12px; margin-bottom:14px; }
.topbar h1 { margin:0; font-size:22px; }
.topbar-center { display:flex; gap:8px; justify-content:center; }
.topbar-actions { display:flex; gap:8px; justify-content:flex-end; }
.power-btn { width:auto; padding:9px 16px; font-size:14px; font-weight:700; background:#4b5563; color:#fff; border:0; border-radius:8px; cursor:pointer; }
.power-btn.danger { background:#a12626; }
.layout {
    display:grid; grid-template-columns:minmax(300px,360px) minmax(0,1fr);
    gap:18px; align-items:start;
}
.card {
    background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:15px; margin-bottom:14px;
}
.status-grid {
    display:grid; grid-template-columns:auto 1fr; gap:6px 12px; font-size:14px;
}
.status-grid .k { color:var(--muted); }
.row { margin:13px 0; }
.row label.title { display:flex; justify-content:space-between; gap:8px; font-weight:600; margin-bottom:5px; }
.val { color:var(--muted); font-weight:500; font-variant-numeric:tabular-nums; }
input[type=range] { width:100%; }
select, input[type=number] {
    width:100%; padding:8px; border:1px solid var(--line); border-radius:7px;
    background:white; color:var(--text); font-size:14px;
}
.checks { display:flex; gap:18px; flex-wrap:wrap; margin:12px 0; }
button {
    width:100%; border:0; border-radius:9px; padding:13px;
    background:var(--primary); color:white; font-size:17px; font-weight:700; cursor:pointer;
}
button:disabled { opacity:.55; cursor:wait; }
.notice { padding:9px 10px; border-radius:8px; margin-bottom:10px; font-size:14px; }
.ok { background:#e8f7ee; color:var(--good); }
.err { background:#fdeaea; color:var(--bad); white-space:pre-wrap; max-height:250px; overflow:auto; }
.media-grid { display:grid; grid-template-columns:1fr 1fr; gap:14px; align-items:start; }
.media {
    width:100%; aspect-ratio:4 / 3; background:#111; border-radius:9px;
    overflow:hidden; display:flex; align-items:center; justify-content:center;
}
.media img { display:block; width:100%; height:100%; object-fit:contain; }
.media.off { width:56px; height:56px; aspect-ratio:auto; background:#000; }
/* 90/270 are done in software for the still and with CSS for the live preview,
   so the frame box becomes portrait. */
.media.rot90, .media.rot270 { aspect-ratio:3 / 4; }
.media.rot90 img.rot { width:133.34%; height:75%; transform:rotate(90deg); }
.media.rot270 img.rot { width:133.34%; height:75%; transform:rotate(-90deg); }
.card-head { display:flex; align-items:center; justify-content:space-between; gap:10px; }
.toggle { font-size:13px; font-weight:600; color:var(--muted); display:flex; align-items:center; gap:6px; }
.toggle input { width:auto; margin:0; }
.toggle-group { display:flex; gap:12px; align-items:center; }
.image-meta { margin-top:8px; color:var(--muted); font-size:13px; }
.image-meta a { color:inherit; }
details { margin-top:14px; }
summary { cursor:pointer; font-weight:650; }
.mono {
    margin-top:8px; padding:8px; border-radius:7px; background:#f1f3f5;
    font:12px/1.35 ui-monospace,SFMono-Regular,Consolas,monospace;
    overflow-wrap:anywhere;
}
.help { color:var(--muted); font-size:12px; margin-top:4px; }
.manual-block { padding:10px; background:#f7f8fa; border-radius:8px; }
@media(max-width:1100px) {
    .media-grid { grid-template-columns:1fr; }
}
@media(max-width:850px) {
    body { padding:10px 10px 70px; }
    .layout { grid-template-columns:1fr; }
    .topbar { grid-template-columns:1fr; }
    .topbar-center, .topbar-actions { justify-content:flex-start; flex-wrap:wrap; }
}
</style>
<script>
function updateValue(el) {
    const out = document.getElementById(el.name + "_value");
    if (out) out.textContent = el.value;
}
function toggleManual() {
    const box = document.getElementById("manual_exposure");
    const block = document.getElementById("manual_block");
    block.style.opacity = box.checked ? "1" : ".45";
    block.querySelectorAll("input").forEach(x => x.disabled = !box.checked);
}
window.addEventListener("DOMContentLoaded", toggleManual);

function initLight() {
    let lightOn = {{ 'true' if s.light_on else 'false' }};
    const lightToggleBtn = document.getElementById("light_toggle");
    const lightSlider = document.getElementById("light_brightness");
    const lightVal = document.getElementById("light_brightness_value");

    function refreshLightUI() {
        lightToggleBtn.textContent = lightOn ? "Turn off" : "Turn on";
        lightToggleBtn.style.background = lightOn ? "#a12626" : "#2563eb";
    }

    async function postLight(payload) {
        try {
            const res = await fetch("/light", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload),
            });
            const data = await res.json();
            lightOn = data.light_on;
            const pct = Math.round(data.light_brightness * 100);
            lightSlider.value = pct;
            lightVal.textContent = pct + "%";
            refreshLightUI();
        } catch (e) {
            console.error(e);
        }
    }

    lightToggleBtn.addEventListener("click", () => postLight({ on: !lightOn }));

    // The label follows the slider live, but the value is sent only on release:
    // every PWM write re-arms the software PWM and can cause a visible blink.
    lightSlider.addEventListener("input", () => {
        lightVal.textContent = lightSlider.value + "%";
    });
    lightSlider.addEventListener("change", () => {
        postLight({ brightness: lightSlider.value / 100 });
    });

    refreshLightUI();
}

async function postPower(action) {
    const msg = action === "reboot"
        ? "Reboot the Raspberry Pi?"
        : "Shut down the Raspberry Pi?";
    if (!confirm(msg)) return;
    try {
        await fetch("/power", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ action }),
        });
    } catch (e) {
        // the server goes down before answering
    }
    document.body.innerHTML = '<div class="notice" style="padding:20px;font-size:18px;">'
        + (action === "reboot" ? "Rebooting…" : "Shutting down…") + '</div>';
}

function initPower() {
    const rebootBtn = document.getElementById("reboot_btn");
    const shutdownBtn = document.getElementById("shutdown_btn");
    if (rebootBtn) rebootBtn.addEventListener("click", () => postPower("reboot"));
    if (shutdownBtn) shutdownBtn.addEventListener("click", () => postPower("poweroff"));
}

function initRecorder() {
    const start = document.getElementById("rec_start");
    const stop = document.getElementById("rec_stop");
    const state = document.getElementById("rec_state");
    if (start) start.addEventListener("click", async () => {
        try { await fetch("/record/start", { method: "POST" }); } catch (e) {}
        if (state) state.textContent = "recording";
    });
    if (stop) stop.addEventListener("click", async () => {
        try { await fetch("/record/stop", { method: "POST" }); } catch (e) {}
        if (state) state.textContent = "stopped";
    });
}

function initLiveSettings() {
    const form = document.getElementById("capture_form");
    if (!form) return;
    let timer = null;
    const send = () => {
        clearTimeout(timer);
        timer = setTimeout(async () => {
            try {
                await fetch("/controls", { method: "POST", body: new FormData(form) });
            } catch (e) {}
        }, 250);
    };
    form.addEventListener("input", send);
    form.addEventListener("change", send);
}

window.addEventListener("DOMContentLoaded", initLight);
window.addEventListener("DOMContentLoaded", initPower);
window.addEventListener("DOMContentLoaded", initRecorder);
function initPreviewToggle() {
    const box = document.querySelector('input[name="preview_enabled"]');
    const wrap = document.getElementById("preview_media");
    if (!box) return;
    box.addEventListener("change", () => {
        if (wrap) {
            wrap.classList.toggle("off", !box.checked);
            wrap.innerHTML = box.checked
                ? '<img id="preview_img" src="/stream" alt="Live preview">'
                : '';
        }
        const form = document.getElementById("capture_form");
        if (form) {
            fetch("/controls", { method: "POST", body: new FormData(form) }).catch(() => {});
        }
    });
}

function initCapture() {
    const form = document.getElementById("capture_form");
    const btn = document.getElementById("capture_btn");
    const msg = document.getElementById("capture_msg");
    if (!form) return;
    form.addEventListener("submit", async (e) => {
        e.preventDefault();  // keep the page, so open sections stay open
        if (btn) { btn.disabled = true; btn.textContent = "Capturing…"; }
        if (msg) { msg.style.display = "none"; }
        try {
            const res = await fetch("/capture", { method: "POST", body: new FormData(form) });
            const data = await res.json();
            if (data.image_url) {
                const img = document.getElementById("still_img");
                if (img) img.src = data.image_url;
            }
            const meta = document.getElementById("still_meta");
            if (meta) {
                meta.innerHTML = (data.image_info ? "<br>" + data.image_info : "")
                    + (data.raw_info ? "<br>" + data.raw_info : "");
            }
            if (msg) {
                msg.className = "notice " + (data.ok ? "ok" : "err");
                msg.textContent = data.ok ? ("Photo captured. " + (data.info || "")) : (data.error || "Capture failed");
                msg.style.display = "";
            }
        } catch (err) {
            if (msg) { msg.className = "notice err"; msg.textContent = String(err); msg.style.display = ""; }
        } finally {
            if (btn) { btn.disabled = false; btn.textContent = "Take photo"; }
        }
    });
}

function initFocusToggle() {
    const box = document.querySelector('input[name="focus_mode"]');
    const img = document.getElementById("preview_img");
    if (!box) return;
    box.addEventListener("change", async () => {
        // Save first, then switch the source: /stream refuses to run while focus
        // mode is on, so asking for it before the setting is stored would leave
        // a blank image behind.
        const form = document.getElementById("capture_form");
        if (form) {
            try { await fetch("/controls", { method: "POST", body: new FormData(form) }); }
            catch (e) {}
        }
        if (img) {
            img.src = box.checked
                ? ("/focus?t=" + Date.now())
                : ("/stream?t=" + Date.now());
        }
    });
}

function initTimelapse() {
    const start = document.getElementById("tl_start");
    const stop = document.getElementById("tl_stop");
    const sync = document.getElementById("nas_sync");
    const info = document.getElementById("tl_status");
    const interval = document.getElementById("tl_interval");
    const unit = document.getElementById("tl_interval_unit");

    async function refresh() {
        try {
            const s = await (await fetch("/timelapse/state")).json();
            if (!info) return;
            let text;
            if (s.active) {
                text = "running · " + s.session + " · frames " + s.frames + " · next in " + s.countdown_s + "s";
            } else if (s.frames) {
                text = "stopped · " + s.session + " · frames " + s.frames + (s.last_error ? " · " + s.last_error : "");
            } else {
                text = "idle";
            }
            text += " · " + s.free_percent + "% free";
            if (s.nas_enabled) {
                text += " · NAS " + (s.nas_ready ? "ready" : (s.nas_reason || "unavailable"));
                if (s.nas_pending) { text += ", queued " + s.nas_pending; }
            }
            info.textContent = text;
        } catch (e) {}
    }
    if (start) start.addEventListener("click", async () => {
        const body = new FormData();
        const amount = interval ? (parseFloat(interval.value) || 1) : 60;
        const factor = unit ? (parseFloat(unit.value) || 1) : 1;
        body.append("interval_s", String(Math.max(1, Math.round(amount * factor))));
        try { await fetch("/timelapse/start", { method: "POST", body }); } catch (e) {}
        refresh();
    });
    if (stop) stop.addEventListener("click", async () => {
        try { await fetch("/timelapse/stop", { method: "POST" }); } catch (e) {}
        refresh();
    });
    if (sync) sync.addEventListener("click", async () => {
        sync.disabled = true;
        try {
            const r = await (await fetch("/nas/sync", { method: "POST" })).json();
            if (info) {
                const head = r.ok
                    ? ("NAS: uploaded " + r.uploaded + ", queued " + r.pending)
                    : ("NAS: " + (r.reason || "unavailable") + ", queued " + r.pending);
                info.textContent = head
                    + (r.pruned ? (", cleared " + r.pruned) : "")
                    + (r.free_percent !== undefined ? (" · " + r.free_percent + "% free") : "");
            }
        } catch (e) {}
        sync.disabled = false;
        refresh();
    });
    refresh();
    setInterval(refresh, 5000);
}

window.addEventListener("DOMContentLoaded", initLiveSettings);
window.addEventListener("DOMContentLoaded", initPreviewToggle);
window.addEventListener("DOMContentLoaded", initCapture);
window.addEventListener("DOMContentLoaded", initFocusToggle);
window.addEventListener("DOMContentLoaded", initTimelapse);
</script>
</head>
<body>
<div class="topbar">
<h1>Silkworm Pi Camera</h1>
<div class="topbar-center">
<button type="submit" form="capture_form" class="power-btn" id="capture_btn">Take photo</button>
<button type="button" class="power-btn" id="rec_start">Record</button>
<button type="button" class="power-btn danger" id="rec_stop">Stop</button>
</div>
<div class="topbar-actions">
<button type="button" class="power-btn" id="reboot_btn">Reboot</button>
<button type="button" class="power-btn danger" id="shutdown_btn">Shutdown</button>
</div>
</div>

<div class="layout">
<section>
<div class="card">
<h2>Capture</h2>
{% if message %}<div class="notice ok">{{ message }}</div>{% endif %}
{% if error %}<div class="notice err">{{ error }}</div>{% endif %}

<form id="capture_form" method="post">
<div id="capture_msg" class="notice ok" style="display:none;"></div>
<div class="checks">
<label><input type="checkbox" name="save_raw" {% if s.save_raw %}checked{% endif %}> Also save RAW (DNG)</label>
</div>
<div class="help">The settings below are applied to the live preview immediately.</div>

<details>
<summary>Image settings</summary>
<div class="row">
<label class="title">Preview bitrate, Mbps</label>
<input type="number" name="preview_bitrate" min="0" max="50" step="1" value="{{ s.preview_bitrate }}">
<div class="help">Quality of the preview stream: higher = sharper, more traffic (0 = auto). Changing it restarts the camera.</div>
</div>

<div class="row">
<label class="title">Focus crop</label>
<select name="focus_crop">
{% for key in focus_crops %}
<option value="{{ key }}" {% if s.focus_crop == key %}selected{% endif %}>{{ key }}</option>
{% endfor %}
</select>
<div class="help">1:1 centre crop for focus mode - no scaling. 320x240 = strongest magnification, 1280x960 = more of the frame. Beyond 1:1 does not help.</div>
</div>
<div class="row">
<label class="title">Resolution</label>
<select name="resolution">
{% for key, item in resolutions.items() %}
<option value="{{ key }}" {% if s.resolution == key %}selected{% endif %}>{{ key }} — {{ item[2] }}</option>
{% endfor %}
</select>
</div>

{% for name,title,minv,maxv,step in sliders %}
<div class="row">
<label class="title">{{ title }} <span class="val" id="{{ name }}_value">{{ s[name] }}</span></label>
<input type="range" name="{{ name }}" min="{{ minv }}" max="{{ maxv }}" step="{{ step }}"
       value="{{ s[name] }}" oninput="updateValue(this)">
</div>
{% endfor %}

<div class="row">
<label class="title">Rotation</label>
<select name="rotation">
<option value="0" {% if s.rotation == 0 %}selected{% endif %}>0°</option>
<option value="90" {% if s.rotation == 90 %}selected{% endif %}>90°</option>
<option value="180" {% if s.rotation == 180 %}selected{% endif %}>180°</option>
<option value="270" {% if s.rotation == 270 %}selected{% endif %}>270°</option>
</select>
</div>

<div class="checks">
<label><input type="checkbox" name="hflip" {% if s.hflip %}checked{% endif %}> H flip</label>
<label><input type="checkbox" name="vflip" {% if s.vflip %}checked{% endif %}> V flip</label>
</div>
</details>

<details>
<summary>Manual exposure &amp; advanced</summary>

<div class="row">
<label class="title">White balance</label>
<select name="awb">
{% for x in awb_modes %}<option value="{{ x }}" {% if s.awb == x %}selected{% endif %}>{{ x }}</option>{% endfor %}
</select>
</div>

<div class="row">
<label class="title">Metering</label>
<select name="metering">
{% for x in metering_modes %}<option value="{{ x }}" {% if s.metering == x %}selected{% endif %}>{{ x }}</option>{% endfor %}
</select>
</div>

<div class="row">
<label class="title">Exposure profile</label>
<select name="exposure">
{% for x in exposure_modes %}<option value="{{ x }}" {% if s.exposure == x %}selected{% endif %}>{{ x }}</option>{% endfor %}
</select>
</div>

<div class="row">
<label class="title">Denoise</label>
<select name="denoise">
{% for x in denoise_modes %}<option value="{{ x }}" {% if s.denoise == x %}selected{% endif %}>{{ x }}</option>{% endfor %}
</select>
</div>

<div class="checks">
<label><input id="manual_exposure" type="checkbox" name="manual_exposure"
              onchange="toggleManual()" {% if s.manual_exposure %}checked{% endif %}> Manual exposure</label>
</div>

<div id="manual_block" class="manual-block">
<div class="row">
<label class="title">Shutter</label>
<select name="shutter">
{% for label, us in shutter_speeds %}
<option value="{{ us }}" {% if s.shutter == us %}selected{% endif %}>{{ label }} ({{ us }} μs)</option>
{% endfor %}
</select>
<div class="help">Exposure time. 1/100 s = 10 000 μs.</div>
</div>

<div class="row">
<label class="title">Analogue gain <span class="val" id="gain_value">{{ s.gain }}</span></label>
<input type="range" name="gain" min="1" max="16" step="0.1"
       value="{{ s.gain }}" oninput="updateValue(this)">
</div>
</div>
</details>

</form>
</div>

<div class="card">
<h2>Timelapse</h2>
<div class="topbar-actions" style="justify-content:flex-start;">
<button type="button" class="power-btn" id="tl_start">Start</button>
<button type="button" class="power-btn danger" id="tl_stop">Stop</button>
<button type="button" class="power-btn" id="nas_sync">Sync now</button>
</div>
<div class="help" id="tl_status">idle</div>
<details>
<summary>Timelapse settings</summary>
<div class="row">
<label class="title">Interval</label>
<input type="number" id="tl_interval" min="1" step="1" value="60">
<select id="tl_interval_unit">
<option value="1">seconds</option>
<option value="60">minutes</option>
<option value="3600">hours</option>
</select>
<div class="help">One frame per interval, at full sensor resolution. "Also save RAW" above is honoured, and a run resumes by itself after a reboot or power cut.</div>
</div>
<div class="checks">
<label><input type="checkbox" name="nas_enabled" form="capture_form" {% if s.nas_enabled %}checked{% endif %}> Copy frames to NAS</label>
</div>
<div class="row">
<label class="title">NAS folder</label>
<input type="text" name="nas_dir" form="capture_form" value="{{ s.nas_dir }}" placeholder="/mnt/nas/silkworm" spellcheck="false" autocomplete="off">
<div class="help">Mount point of the NAS share on the Pi. Frames are always written locally first and copied here afterwards, so an unreachable NAS never loses a frame - the card keeps them until the share is back. On the NAS each run lands in its own dated folder with a session.json describing how it was shot.</div>
</div>
<div class="row">
<label class="title">Clear the card below, % free</label>
<input type="number" name="nas_min_free_percent" form="capture_form" min="0" max="50" step="1" value="{{ s.nas_min_free_percent }}">
<div class="help">Frames stay on the card as long as there is room. Once free space falls below this, the oldest frames that are already safely on the NAS are cleared first. 0 = never clear automatically.</div>
</div>
</details>
</div>

<div class="card">
<h2>Light</h2>
<div class="row">
<label class="title">Lamp brightness <span class="val" id="light_brightness_value">{{ (s.light_brightness * 100) | int }}%</span></label>
<input type="range" id="light_brightness" min="0" max="100" step="1"
       value="{{ (s.light_brightness * 100) | int }}">
</div>
<button type="button" id="light_toggle">Turn on</button>
<div class="help">Lamp via TB6612 driver: AIN1=23, AIN2=24, STBY=25, PWM=18 — {{ 'hardware' if light_pwm_hw else 'software' }} PWM.</div>
</div>
</section>

<div class="media-grid">
<section class="card">
<h2 class="card-head">
<span>Live preview</span>
<span class="toggle-group">
<label class="toggle"><input type="checkbox" name="focus_mode" form="capture_form" {% if s.focus_mode %}checked{% endif %}> focus</label>
<label class="toggle"><input type="checkbox" name="preview_enabled" form="capture_form" {% if s.preview_enabled %}checked{% endif %}> on</label>
</span>
</h2>
{% if not camera_ok %}
<div class="media"><span style="color:#aaa;">picamera2 is not available</span></div>
{% elif not s.preview_enabled %}
<div class="media off" id="preview_media" title="Preview is off"></div>
{% else %}
<div class="media {{ rot_class }}" id="preview_media"><img class="rot" id="preview_img" src="{{ '/focus' if s.focus_mode else '/stream' }}" alt="Live preview"></div>
{% endif %}
<div class="image-meta">
Live MJPEG from the camera. Keeps running while you take photos.
Recorder: <b id="rec_state">{{ 'recording' if recording else 'idle' }}</b>
</div>
</section>

<section class="card">
<h2>Latest image</h2>
{% if image_url %}
<div class="media {{ rot_class }}">
<a href="/image" target="_blank" title="Open original full-size image">
<img id="still_img" src="{{ image_url }}" alt="Latest photo">
</a>
</div>
<div class="image-meta">
Click the image to open the original.
<span id="still_meta">{% if image_info %}<br>{{ image_info }}{% endif %}{% if raw_info %}<br>{{ raw_info }}{% endif %}</span>
</div>
{% else %}
<div class="media {{ rot_class }}" style="color:#aaa;">No photo yet</div>
{% endif %}
</section>
</div>
</div>

<footer class="statusbar">
<span><b>Temp</b> {{ status.temp }}</span>
<span><b>Throttle</b> {{ status.throttled }}</span>
<span><b>Uptime</b> {{ status.uptime }}</span>
<span><b>Disk</b> {{ status.disk_free }}</span>
<span><b>Last photo</b> {{ status.last_photo }}</span>
</footer>
</body>
</html>
"""

SLIDERS = [
    ("quality", "JPEG quality", 50, 100, 1),
    ("brightness", "Brightness", -1, 1, 0.1),
    ("contrast", "Contrast", 0, 2, 0.1),
    ("saturation", "Saturation", 0, 2, 0.1),
    ("sharpness", "Sharpness", 0, 4, 0.1),
    ("ev", "EV compensation", -4, 4, 0.5),
]


def run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def text_cmd(cmd, fallback="n/a"):
    try:
        r = run(cmd)
        txt = (r.stdout or r.stderr).strip()
        return txt or fallback
    except Exception:
        return fallback


def status():
    _, _, free = shutil.disk_usage("/")
    last = "—"
    if IMAGE_PATH.exists():
        last = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(IMAGE_PATH.stat().st_mtime),
        )
    return {
        "temp": text_cmd(["vcgencmd", "measure_temp"]),
        "throttled": text_cmd(["vcgencmd", "get_throttled"]),
        "uptime": text_cmd(["uptime", "-p"]),
        "disk_free": f"{free / (1024**3):.1f} GB",
        "last_photo": last,
    }


def fval(name, default, lo, hi):
    try:
        x = float(request.form.get(name, default))
        return max(lo, min(hi, x))
    except Exception:
        return default


def ival(name, default, lo, hi):
    try:
        x = int(float(request.form.get(name, default)))
        return max(lo, min(hi, x))
    except Exception:
        return default


def read_form():
    resolution = request.form.get("resolution", SETTINGS["resolution"])
    if resolution not in RESOLUTIONS:
        resolution = "1640x1232"

    SETTINGS.update({
        "resolution": resolution,
        "quality": ival("quality", SETTINGS["quality"], 50, 100),
        "brightness": fval("brightness", SETTINGS["brightness"], -1, 1),
        "contrast": fval("contrast", SETTINGS["contrast"], 0, 2),
        "saturation": fval("saturation", SETTINGS["saturation"], 0, 2),
        "sharpness": fval("sharpness", SETTINGS["sharpness"], 0, 4),
        "ev": fval("ev", SETTINGS["ev"], -4, 4),
        "hflip": request.form.get("hflip") == "on",
        "vflip": request.form.get("vflip") == "on",
        "manual_exposure": request.form.get("manual_exposure") == "on",
        "save_raw": request.form.get("save_raw") == "on",
    })

    try:
        rotation = int(float(request.form.get(
            "rotation", SETTINGS.get("rotation", 0)) or 0)) % 360
    except Exception:
        rotation = 0
    SETTINGS["rotation"] = rotation if rotation in (0, 90, 180, 270) else 0

    awb = request.form.get("awb", SETTINGS["awb"])
    metering = request.form.get("metering", SETTINGS["metering"])
    exposure = request.form.get("exposure", SETTINGS["exposure"])
    denoise = request.form.get("denoise", SETTINGS["denoise"])

    try:
        bitrate = float(request.form.get("preview_bitrate",
                        SETTINGS.get("preview_bitrate", 15)) or 0)
    except Exception:
        bitrate = 15.0
    SETTINGS["preview_bitrate"] = max(0.0, min(50.0, bitrate))
    SETTINGS["preview_enabled"] = request.form.get("preview_enabled") == "on"
    focus_before = bool(SETTINGS.get("focus_mode"))
    SETTINGS["focus_mode"] = request.form.get("focus_mode") == "on"
    # NAS export. The address and credentials stay in the Pi's /etc/fstab; the app
    # only ever sees the local mount point, so nothing network-specific is stored
    # here. Empty means "keep frames locally only".
    SETTINGS["nas_enabled"] = request.form.get("nas_enabled") == "on"
    SETTINGS["nas_dir"] = str(
        request.form.get("nas_dir", SETTINGS.get("nas_dir", ""))).strip()
    try:
        min_free = float(request.form.get(
            "nas_min_free_percent",
            SETTINGS.get("nas_min_free_percent", 10)) or 0)
    except Exception:
        min_free = 10.0
    SETTINGS["nas_min_free_percent"] = max(0.0, min(50.0, min_free))
    focus_crop = request.form.get("focus_crop", SETTINGS.get("focus_crop", "640x480"))
    SETTINGS["focus_crop"] = focus_crop if focus_crop in FOCUS_CROPS else "640x480"

    SETTINGS["awb"] = awb if awb in AWB_MODES else "auto"
    SETTINGS["metering"] = metering if metering in METERING_MODES else "centre"
    SETTINGS["exposure"] = exposure if exposure in EXPOSURE_MODES else "normal"
    SETTINGS["denoise"] = denoise if denoise in DENOISE_MODES else "fast"

    if SETTINGS["manual_exposure"]:
        SETTINGS["shutter"] = ival("shutter", SETTINGS["shutter"], 100, 100000)
        SETTINGS["gain"] = fval("gain", SETTINGS["gain"], 1, 16)

    preview_before = bool(SETTINGS.get("preview_enabled"))
    if not SETTINGS["focus_mode"]:
        # Leaving focus mode has to free the preview at once. The block window is
        # up to FOCUS_BLOCK_S long, and a /stream request landing inside it gets a
        # 200 with no frames - which the browser shows as a permanently black
        # image, because it never asks again.
        camera.preview_blocked_until = 0.0

    save_settings()
    camera.set_preview_enabled(SETTINGS["preview_enabled"])
    # Switching the preview or focus mode off has to restart the camera rather
    # than just stop the encoder: an encoder stopped on its own leaves the camera
    # in a state where the next still capture blocks for ever.
    camera.reconfigure(
        camera_size(),
        preview_size=preview_size(),
        preview_bitrate=SETTINGS["preview_bitrate"],
        force=(bool(SETTINGS["focus_mode"]) != bool(focus_before)
               or bool(SETTINGS["preview_enabled"]) != preview_before),
        **orientation_flags(),
    )
    camera.apply_controls(effective_controls())


def capture():
    """Take a still with picamera2; the live preview keeps running."""
    with capture_lock:
        if not cam.AVAILABLE:
            return False, "picamera2 is not available on this host", ""
        if not camera.running:
            camera.start(camera_size())
        camera.apply_controls(effective_controls())

        if TMP_IMAGE_PATH.exists():
            try:
                TMP_IMAGE_PATH.unlink()
            except Exception:
                pass

        raw_target = RAW_PATH if SETTINGS.get("save_raw") else None
        ok, err = camera.capture(
            TMP_IMAGE_PATH,
            quality=SETTINGS["quality"],
            raw_path=raw_target,
            target_size=still_output_size(),
            rotate=SETTINGS.get("rotation", 0),
        )
        if ok and TMP_IMAGE_PATH.exists():
            os.replace(TMP_IMAGE_PATH, IMAGE_PATH)
            info = f"picamera2 · preset {SETTINGS['resolution']}"
            if raw_target is not None:
                info += " · +RAW (latest.dng)"
            return True, "", info

        if TMP_IMAGE_PATH.exists():
            try:
                TMP_IMAGE_PATH.unlink()
            except Exception:
                pass
        return False, err or "Capture failed", ""


# ---------------------------------------------------------------- timelapse
TIMELAPSE_DIR = BASE_DIR / "timelapse"
TL_STATE_PATH = TIMELAPSE_DIR / "state.json"
_tl_lock = threading.Lock()
_tl_state = {}


def timelapse_load():
    """Read state.json; on start-up this is what resumes an interrupted run."""
    global _tl_state
    state = {}
    try:
        if TL_STATE_PATH.exists():
            state = json.loads(TL_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("active", False)
    state.setdefault("interval_s", 60.0)
    state.setdefault("frames", 0)
    state.setdefault("next_shot_at", 0.0)
    state.setdefault("last_shot_at", 0.0)
    state.setdefault("session", "")
    state.setdefault("save_raw", False)
    state.setdefault("quality", 93)
    state.setdefault("min_free_mb", 500)
    state.setdefault("last_error", "")
    with _tl_lock:
        _tl_state = state
    return state


def timelapse_save():
    """Atomic write: a power cut can never leave a half-written state file."""
    try:
        TIMELAPSE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TL_STATE_PATH.with_name("state.json.tmp")
        tmp.write_text(json.dumps(_tl_state, indent=2), encoding="utf-8")
        os.replace(tmp, TL_STATE_PATH)
    except Exception:
        pass


def timelapse_active():
    with _tl_lock:
        return bool(_tl_state.get("active"))


def timelapse_status():
    with _tl_lock:
        state = dict(_tl_state)
    state["interval_s"] = max(1.0, float(state.get("interval_s", 60) or 60))
    next_at = float(state.get("next_shot_at", 0) or 0)
    state["countdown_s"] = max(0, int(round(next_at - time.time()))) if state.get("active") else 0
    state["dir"] = str(TIMELAPSE_DIR)
    nas_ready, nas_reason = nas_check()
    state["nas_enabled"] = bool(SETTINGS.get("nas_enabled"))
    state["nas_ready"] = nas_ready
    state["nas_reason"] = nas_reason
    state["nas_pending"] = nas_pending()
    state["free_percent"] = round(disk_free_percent(), 1)
    state["min_free_percent"] = _number(
        SETTINGS.get("nas_min_free_percent"), 10.0, 0.0, 50.0)
    return state


def _new_session_name():
    """A folder name no earlier run has used.

    The timestamp has one-second resolution, so a restart inside the same second
    would otherwise reuse the folder, reset the frame counter and overwrite
    frame_000001.jpg - leaving the archive holding the stale image for ever.
    """
    base = time.strftime("tl-%Y%m%d-%H%M%S")
    name, n = base, 1
    while (TIMELAPSE_DIR / name).exists() or name == str(
            _tl_state.get("session") or ""):
        n += 1
        name = f"{base}-{n}"
    return name


def timelapse_write_meta(session=None):
    """Record how and when a run was shot, next to its frames.

    The file travels to the NAS with the frames, so a folder opened months later
    still says which settings produced it and when the run started and stopped.
    """
    with _tl_lock:
        state = dict(_tl_state)
    session = session or state.get("session")
    if not session:
        return
    folder = TIMELAPSE_DIR / session
    if not folder.is_dir():
        return
    manual = bool(SETTINGS.get("manual_exposure"))
    meta = {
        "session": session,
        "started_at": state.get("started_at", ""),
        "ended_at": state.get("ended_at", ""),
        "frames": int(state.get("frames", 0) or 0),
        "interval_s": float(state.get("interval_s", 60) or 60),
        "resolution": SETTINGS.get("resolution"),
        "save_raw": bool(state.get("save_raw")),
        "quality": int(state.get("quality", 93) or 93),
        "rotation": SETTINGS.get("rotation"),
        "hflip": SETTINGS.get("hflip"),
        "vflip": SETTINGS.get("vflip"),
        "exposure_mode": SETTINGS.get("exposure"),
        "shutter_us": SETTINGS.get("shutter") if manual else None,
        "gain": SETTINGS.get("gain") if manual else None,
        "denoise": SETTINGS.get("denoise"),
        "awb": SETTINGS.get("awb"),
        "metering": SETTINGS.get("metering"),
        "sharpness": SETTINGS.get("sharpness"),
        "light_on": SETTINGS.get("light_on"),
        "light_brightness": SETTINGS.get("light_brightness"),
    }
    try:
        tmp = folder / (SESSION_META + ".tmp")
        tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, folder / SESSION_META)
    except OSError:
        pass


def timelapse_start(interval_s):
    session = _new_session_name()
    with _tl_lock:
        _tl_state.update({
            "active": True,
            "session": session,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "ended_at": "",
            "interval_s": max(1.0, float(interval_s)),
            "save_raw": bool(SETTINGS.get("save_raw")),
            "quality": int(SETTINGS.get("quality", 93)),
            "frames": 0,
            "last_shot_at": 0.0,
            "next_shot_at": time.time(),
            "min_free_mb": 500,
            "last_error": "",
        })
    timelapse_save()
    timelapse_write_meta(session)
    return timelapse_status()


def timelapse_stop():
    with _tl_lock:
        _tl_state["active"] = False
        _tl_state["ended_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    timelapse_save()
    timelapse_write_meta()
    return timelapse_status()


# ------------------------------------------------------------------ NAS export
# Frames are always written locally first and pushed to the NAS afterwards, so a
# NAS that is slow, unreachable or asleep can never lose a frame. The card acts
# as a cache in front of the NAS: frames stay there while there is room, and once
# free space falls below `nas_min_free_percent` the oldest frames that are already
# safely on the NAS are cleared away. state.json never leaves the Pi.
NAS_RETRY_S = 30.0            # after a failure
NAS_MIN_INTERVAL_S = 5.0      # between ordinary sweeps
NAS_IDLE_RESCAN_S = 30.0      # between rescans while the NAS is unreachable
NAS_PRUNE_BACKOFF_S = 60.0    # after a pass that could not free anything
_nas_lock = threading.Lock()
_nas_next_try = 0.0           # monotonic deadlines: the Pi has no RTC
_nas_prune_next = 0.0
_nas_pending = 0              # cached, so the status endpoint stays cheap
# Session records already on the NAS, keyed by path -> (mtime, size), so a file
# that changes as the run progresses is re-sent and an unchanged one is not.
_nas_meta_sent = {}
# Sessions with nothing left to upload. Only the session being written to can
# gain frames, and that one is dropped from here on every shot, so a sweep can
# skip re-listing thousands of already archived files.
_nas_mirrored = set()


def _number(value, default, lo, hi):
    """A setting as a clamped float, whatever it happens to hold."""
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def nas_target():
    """The NAS mount point on the Pi, resolved, or None if not configured."""
    raw = str(SETTINGS.get("nas_dir") or "").strip()
    if not raw:
        return None
    try:
        return Path(raw).expanduser().resolve()
    except OSError:
        return None


def nas_check():
    """Return (ready, reason). A couple of stat calls when configured.

    The folder has to be an actual mount point, not merely a directory that
    exists. If the share is not mounted the path is an ordinary folder on the SD
    card: frames would be copied onto the card a second time, and the rotation
    would then delete the "original" because the copy looks safe. Pointing it at
    the cache itself is refused for the same reason - there the copy *is* the
    original.
    """
    if not SETTINGS.get("nas_enabled"):
        return False, "disabled"
    target = nas_target()
    if target is None:
        return False, "no folder set"
    if not target.is_dir():
        return False, f"{target} is not there"
    if not os.path.ismount(str(target)):
        return False, f"{target} is not a mount point"
    try:
        cache = TIMELAPSE_DIR.resolve()
        if target == cache or cache in target.parents:
            return False, f"{target} is inside the local cache"
    except OSError:
        pass
    return True, ""


def disk_free_percent():
    """Free space on the filesystem holding the app data, in percent."""
    try:
        total, _, free = shutil.disk_usage(BASE_DIR)
        return 100.0 * free / total if total else 100.0
    except Exception:
        return 100.0


def nas_pending():
    """Files still waiting for the NAS as of the last sweep. Cheap on purpose."""
    return _nas_pending


def nas_sessions():
    """Local session folders, oldest first (the names sort chronologically)."""
    if not TIMELAPSE_DIR.is_dir():
        return []
    try:
        return sorted(p for p in TIMELAPSE_DIR.iterdir()
                      if p.is_dir() and p.name.startswith("tl-"))
    except Exception:
        return []


def _frame_files(session_dir):
    """Frame files in one session folder, ignoring anything half-written.

    A frame is written to a temporary name and renamed into place, so a file
    under its final name is always complete - which is what lets a sweep trust a
    directory listing instead of re-reading every file.
    """
    try:
        return sorted(p for p in session_dir.iterdir()
                      if p.is_file() and not _is_temp_name(p.name))
    except OSError:
        return []


def _is_temp_name(name):
    return name.endswith((".tmp", ".part")) or ".tmp." in name


def _local_file_count():
    return sum(len(_frame_files(s)) for s in nas_sessions())


SESSION_META = "session.json"


def nas_session_dir(target, session):
    """Where a session lives on the NAS: <base>/<YYYY-MM-DD>/<session>.

    Grouped by day, so a camera left running for months still produces a tree a
    person can walk through instead of one flat directory of thousands of
    sessions. The date comes from the session name, so the layout is stable and
    a given frame always lands in the same place.
    """
    stamp = session[3:11]                # tl-YYYYMMDD-HHMMSS
    if len(stamp) == 8 and stamp.isdigit():
        day = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:]}"
    else:
        day = time.strftime("%Y-%m-%d")
    return target / day / session


def _meta_stamp(path):
    """A digest of the session record, or None if it is unreadable.

    Content rather than (mtime, size): timestamps can be coarse - tmpfs ticks in
    milliseconds and an SD card in whole seconds - so a record rewritten twice
    inside one tick with the same length would look unchanged and never be sent
    again. The file is only a few hundred bytes, so hashing it is cheap.
    """
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _nas_listing(session_dir):
    """Names already on the NAS for one session - a single listing call.

    _nas_put renames only after checking the size, so anything present under its
    final name is complete and never needs uploading a second time.
    """
    try:
        return {p.name for p in session_dir.iterdir()}
    except OSError:
        return set()


def _nas_put(src, dst):
    """Copy one file to the NAS: temp name, verify the size, then rename.

    The rename keeps half-written files off the NAS if the link drops mid-copy,
    and it is what lets a sweep tell what is missing from a directory listing.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".part")
    shutil.copyfile(src, tmp)
    try:
        if tmp.stat().st_size != src.stat().st_size:
            raise OSError(f"short write: {dst.name}")
        os.replace(tmp, dst)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def nas_flush(force=False):
    """Upload whatever the NAS is missing, then report what is still local.

    Never raises and never loses a frame: local copies are left in place, so a
    failure just means the next sweep tries again.
    """
    global _nas_next_try, _nas_pending
    now = time.monotonic()
    ready, why = nas_check()
    if not ready:
        _nas_mirrored.clear()       # we can no longer vouch for what is up there
        if force or now >= _nas_next_try:
            _nas_next_try = now + NAS_IDLE_RESCAN_S
            _nas_pending = _local_file_count()
        return {"ok": False, "reason": why, "uploaded": 0,
                "pending": _nas_pending}

    with _nas_lock:                  # also stops a manual sweep from racing this
        todo = [s for s in nas_sessions()
                if force or s.name not in _nas_mirrored]
        if not todo or (not force and now < _nas_next_try):
            _nas_next_try = now + NAS_MIN_INTERVAL_S
            return {"ok": True, "reason": "", "uploaded": 0,
                    "pending": _nas_pending}

        target = nas_target()
        uploaded, pending, failed = 0, 0, ""
        try:
            for session in todo:
                folder = nas_session_dir(target, session.name)
                on_nas = _nas_listing(folder)
                left = 0
                for src in _frame_files(session):
                    stamp = (_meta_stamp(src)
                             if src.name == SESSION_META else None)
                    if stamp is not None:
                        # The session record is rewritten as the run progresses,
                        # so it is re-sent whenever it changes rather than once.
                        if _nas_meta_sent.get(str(src)) == stamp:
                            continue
                    elif src.name in on_nas:
                        continue
                    counted = stamp is None          # the record is not a frame
                    if counted:
                        left += 1
                        pending += 1
                    if failed:
                        continue
                    try:
                        _nas_put(src, folder / src.name)
                    except Exception as exc:      # the NAS went away mid-run
                        failed = str(exc)
                        continue
                    uploaded += 1
                    if stamp is not None:
                        _nas_meta_sent[str(src)] = stamp
                    if counted:
                        left -= 1
                        pending -= 1
                if not left:
                    _nas_mirrored.add(session.name)
        except Exception as exc:
            failed = str(exc)

        _nas_pending = pending
        _nas_next_try = now + (NAS_RETRY_S if failed else NAS_MIN_INTERVAL_S)

    return {"ok": not failed, "reason": failed,
            "uploaded": uploaded, "pending": _nas_pending}


def nas_prune(force=False):
    """Clear the oldest frames that are already on the NAS when space runs low.

    Only runs below `nas_min_free_percent`, and only removes a local file once its
    NAS copy is confirmed to exist with the same size - so the rotation can never
    delete the only copy of a frame. Frames not on the NAS yet are left alone,
    which is what makes an unreachable NAS safe: nothing is deleted and the
    timelapse's own low-disk guard stops the run instead.
    """
    global _nas_prune_next
    percent = _number(SETTINGS.get("nas_min_free_percent"), 10.0, 0.0, 50.0)
    if percent <= 0:
        return 0
    now = time.monotonic()
    if not force and now < _nas_prune_next:
        return 0
    ready, _ = nas_check()
    if not ready:
        return 0
    target = nas_target()
    if disk_free_percent() >= percent:
        _nas_prune_next = 0.0
        return 0

    active = str(_tl_state.get("session") or "")
    pruned = 0
    with _nas_lock:
        for session in nas_sessions():                # oldest first
            if session.name == active:
                continue            # never clear the session being written to
            for src in _frame_files(session):
                dst = nas_session_dir(target, session.name) / src.name
                try:
                    if not dst.is_file() or dst.stat().st_size != src.stat().st_size:
                        continue                      # not safely on the NAS
                except OSError:
                    continue
                try:
                    src.unlink()
                except OSError:
                    continue
                pruned += 1
                if disk_free_percent() >= percent:
                    return pruned
            try:
                session.rmdir()                       # gone once every frame left
            except OSError:
                pass

    # Still below the threshold: whatever is left is either not on the NAS yet or
    # cannot be verified, so repeating this every second cannot gain anything.
    _nas_prune_next = now + NAS_PRUNE_BACKOFF_S
    return pruned


def timelapse_shot():
    """Take one frame and advance the schedule in the state file."""
    with _tl_lock:
        state = dict(_tl_state)
    if not state.get("active"):
        return
    session = state.get("session") or _new_session_name()
    sdir = TIMELAPSE_DIR / session
    index = int(state.get("frames", 0)) + 1
    stem = f"frame_{index:06d}"
    jpg = sdir / f"{stem}.jpg"
    raw = (sdir / f"{stem}.dng") if state.get("save_raw") else None
    # Capture next to the real name and rename into place: a sweep running in
    # another thread must never see, and never publish, a half-written frame.
    tmp_jpg = sdir / f"{stem}.tmp.jpg"
    tmp_raw = (sdir / f"{stem}.tmp.dng") if raw is not None else None

    # An absolute floor of last resort. With NAS export working, the rotation
    # keeps free space above its own percentage, so this only fires when nothing
    # can be freed: no NAS, or frames not confirmed on it yet.
    _, _, free = shutil.disk_usage(BASE_DIR)
    if free < int(state.get("min_free_mb", 500)) * 1024 * 1024:
        with _tl_lock:
            _tl_state["active"] = False
            _tl_state["last_error"] = "stopped: low disk space"
        timelapse_save()
        return

    ok, err = False, ""
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        with capture_lock:
            if not cam.AVAILABLE:
                ok, err = False, "picamera2 is not available"
            else:
                if not camera.running:
                    camera.start(camera_size(), **orientation_flags())
                camera.apply_controls(effective_controls())
                ok, err = camera.capture(
                    tmp_jpg,
                    quality=int(state.get("quality", 93)),
                    raw_path=tmp_raw,
                    target_size=still_output_size(),
                    rotate=SETTINGS.get("rotation", 0),
                )
    except Exception as exc:
        ok, err = False, str(exc)

    if ok:
        try:
            os.replace(tmp_jpg, jpg)
            if tmp_raw is not None and tmp_raw.exists():
                os.replace(tmp_raw, raw)
        except OSError as exc:
            ok, err = False, str(exc)
    if not ok:
        for leftover in (tmp_jpg, tmp_raw):
            if leftover is None:
                continue
            try:
                leftover.unlink()
            except OSError:
                pass
    _nas_mirrored.discard(session)       # this session has new work again

    now = time.time()
    with _tl_lock:
        if ok:
            _tl_state["frames"] = index
            _tl_state["last_shot_at"] = now
        _tl_state["next_shot_at"] = now + max(1.0, float(state.get("interval_s", 60) or 60))
        _tl_state["last_error"] = "" if ok else str(err)
    timelapse_save()
    timelapse_write_meta(session)


def timelapse_tick():
    """One pass of the worker: talk to the NAS, then shoot if a frame is due.

    Returns how long to sleep before the next pass. The NAS work has its own
    guard so that a broken share can never stop the camera from shooting.
    """
    with _tl_lock:
        active = bool(_tl_state.get("active"))
        next_at = float(_tl_state.get("next_shot_at", 0) or 0)
    try:                     # a NAS problem must never stop the camera
        nas_flush()
        nas_prune()
    except Exception:
        pass
    if not active:
        return 1.0
    delay = next_at - time.time()
    if delay > 0:
        return min(1.0, delay)
    timelapse_shot()
    return 0.0


def timelapse_worker():
    """Background loop; resumes from state.json after a reboot or crash.

    Also drains the upload queue, so a backlog left by an unreachable NAS is
    pushed as soon as the NAS is back.
    """
    while True:
        try:
            time.sleep(timelapse_tick())
        except Exception:
            time.sleep(2.0)


@app.route("/", methods=["GET", "POST"])
def index():
    message = ""
    error = ""

    if request.method == "POST":
        read_form()
        ok, error, info = capture()
        if ok:
            message = f"Photo captured. {info}"

    image_url = None
    image_info = ""
    if IMAGE_PATH.exists():
        stamp = IMAGE_PATH.stat().st_mtime_ns
        image_url = f"/image?v={stamp}"
        size_mb = IMAGE_PATH.stat().st_size / (1024 * 1024)
        image_info = f"File: latest.jpg · {size_mb:.2f} MB · preset {SETTINGS['resolution']}"

    raw_info = ""
    if SETTINGS.get("save_raw") and RAW_PATH.exists():
        raw_mb = RAW_PATH.stat().st_size / (1024 * 1024)
        raw_info = f"RAW: latest.dng · {raw_mb:.2f} MB"

    rot = int(SETTINGS.get("rotation", 0)) % 360
    rot_class = "rot90" if rot == 90 else ("rot270" if rot == 270 else "")

    return render_template_string(
        HTML,
        status=status(),
        s=SETTINGS,
        resolutions=RESOLUTIONS,
        sliders=SLIDERS,
        shutter_speeds=SHUTTER_SPEEDS,
        awb_modes=AWB_MODES,
        metering_modes=METERING_MODES,
        exposure_modes=EXPOSURE_MODES,
        denoise_modes=DENOISE_MODES,
        image_url=image_url,
        image_info=image_info,
        raw_info=raw_info,
        image_path=str(IMAGE_PATH),
        message=message,
        error=error,
        camera_ok=cam.AVAILABLE,
        recording=camera.recording,
        light_pwm_hw=LIGHT_PWM_HW,
        rot_class=rot_class,
        focus_crops=FOCUS_CROPS,
    )


@app.route("/capture", methods=["POST"])
def capture_now():
    """AJAX capture: keeps the page (and any expanded sections) untouched."""
    read_form()
    ok, err, info = capture()
    image_url = image_info = raw_info = ""
    if IMAGE_PATH.exists():
        image_url = f"/image?v={IMAGE_PATH.stat().st_mtime_ns}"
        size_mb = IMAGE_PATH.stat().st_size / (1024 * 1024)
        image_info = f"File: latest.jpg · {size_mb:.2f} MB · preset {SETTINGS['resolution']}"
    if SETTINGS.get("save_raw") and RAW_PATH.exists():
        raw_mb = RAW_PATH.stat().st_size / (1024 * 1024)
        raw_info = f"RAW: latest.dng · {raw_mb:.2f} MB"
    return jsonify(
        ok=ok,
        error=err,
        info=info,
        image_url=image_url,
        image_info=image_info,
        raw_info=raw_info,
    )


@app.route("/controls", methods=["POST"])
def controls():
    """Apply settings live (without capturing) so the preview reflects them."""
    read_form()
    return jsonify(ok=True, settings=SETTINGS)


@app.route("/light", methods=["POST"])
def light():
    data = request.get_json(silent=True) or request.form
    if "on" in data:
        SETTINGS["light_on"] = str(data["on"]).lower() in (
            "1", "true", "on", "yes")
    if "brightness" in data:
        try:
            SETTINGS["light_brightness"] = max(
                0.0, min(1.0, float(data["brightness"])))
        except Exception:
            pass
    if "freq" in data:
        try:
            SETTINGS["light_freq"] = max(
                1, min(10000, int(float(data["freq"]))))
        except Exception:
            pass
    save_settings()
    apply_light()
    return jsonify(
        light_on=SETTINGS["light_on"],
        light_brightness=SETTINGS["light_brightness"],
        light_freq=SETTINGS["light_freq"],
    )


@app.route("/image")
def image():
    if not IMAGE_PATH.exists():
        return "No image yet", 404
    return send_file(
        IMAGE_PATH,
        mimetype="image/jpeg",
        max_age=0,
        conditional=False,
    )


@app.route("/stream")
def stream():
    """Live MJPEG preview; keeps running while stills are captured."""
    if not cam.AVAILABLE:
        return "Live preview is not available (picamera2 missing)", 503
    if not SETTINGS.get("preview_enabled", True):
        return "", 204
    if SETTINGS.get("focus_mode"):
        # The focus helper needs the camera to itself; the encoder and
        # capture_array("main") cannot share it. Focus mode wins.
        return "", 204
    if not camera.running:
        camera.start(camera_size(), **orientation_flags())
    return Response(
        camera.frames(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


FOCUS_BLOCK_S = 5.0      # how long the preview stands down for the focus helper


def _focus_begin():
    """Keep new preview requests off the camera while the focus helper uses it.

    The preview encoder is *not* stopped here on purpose. `capture_array` racing
    an asynchronous `stop_recording()` is what deadlocks the camera; instead the
    camera is restarted cleanly when focus mode is switched on, so by the time
    the first frame is asked for there is no encoder left to race.
    """
    camera.preview_blocked_until = time.monotonic() + FOCUS_BLOCK_S
    camera.apply_controls(effective_controls())


def _focus_frames(crop):
    """Stream of 1:1 centre crops taken from full-resolution frames.

    The preview encoder and capture_array("main") cannot use the camera at the
    same time - the same exclusivity that keeps recording and the preview apart.
    The block is refreshed every frame and expires on its own, so a stream that
    ends without cleaning up cannot leave the preview disabled.
    """
    _focus_begin()
    misses = 0
    while True:
        camera.preview_blocked_until = time.monotonic() + FOCUS_BLOCK_S
        data = camera.focus_jpeg(crop)
        if data:
            misses = 0
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + data + b"\r\n"
            continue
        misses += 1
        if misses in (5, 20):
            app.logger.warning(
                "focus stream: %d captures failed in a row (%s)",
                misses, camera.last_error)
        time.sleep(0.2 if misses < 10 else 1.0)


@app.route("/focus/check")
def focus_check():
    """One focus capture as JSON, so the path can be tested without a stream."""
    if not cam.AVAILABLE:
        return jsonify(ok=False, error="picamera2 is not available"), 503
    if not camera.running:
        camera.start(camera_size(), **orientation_flags())
    crop = (640, 480)
    try:
        w, h = str(SETTINGS.get("focus_crop", "640x480")).lower().split("x")
        crop = (int(w), int(h))
    except Exception:
        pass
    started = time.time()
    _focus_begin()
    try:
        count = max(1, min(6, int(request.args.get("frames", 1))))
    except Exception:
        count = 1
    times, shots, ok = [], 0, True
    for _ in range(count):
        camera.preview_blocked_until = time.monotonic() + FOCUS_BLOCK_S
        step = time.time()
        data = camera.focus_jpeg(crop)
        times.append(int((time.time() - step) * 1000))
        if not data:
            ok = False
            break
        shots += 1
    return jsonify(
        ok=ok,
        error=camera.last_error,
        frames=shots,
        bytes=len(data or b""),
        ms=times,
        total_ms=int((time.time() - started) * 1000),
        crop=f"{crop[0]}x{crop[1]}",
        camera={
            "running": camera.running,
            "streaming": camera.streaming,
            "blocked_for_s": round(
                max(0.0, camera.preview_blocked_until - time.monotonic()), 1),
        },
    )


@app.route("/focus")
def focus():
    """Focus helper: 1:1 centre crops, no scaling, low frame rate."""
    if not cam.AVAILABLE:
        return "Live preview is not available (picamera2 missing)", 503
    if not camera.running:
        camera.start(camera_size(), **orientation_flags())
    crop = (640, 480)
    try:
        w, h = str(SETTINGS.get("focus_crop", "640x480")).lower().split("x")
        crop = (int(w), int(h))
    except Exception:
        pass
    return Response(
        _focus_frames(crop),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/timelapse/state")
def timelapse_state_route():
    return jsonify(timelapse_status())


@app.route("/timelapse/start", methods=["POST"])
def timelapse_start_route():
    data = request.get_json(silent=True) or request.form
    try:
        interval = float(data.get("interval_s") or 60)
    except Exception:
        interval = 60.0
    return jsonify(timelapse_start(interval))


@app.route("/timelapse/stop", methods=["POST"])
def timelapse_stop_route():
    return jsonify(timelapse_stop())


@app.route("/nas/sync", methods=["POST"])
def nas_sync_route():
    """Sweep now, ignoring the regular interval."""
    result = nas_flush(force=True)
    result["pruned"] = nas_prune(force=True)
    result["free_percent"] = round(disk_free_percent(), 1)
    return jsonify(result)


@app.route("/record/start", methods=["POST"])
def record_start():
    if not cam.AVAILABLE:
        return jsonify(error="picamera2 is not available"), 503
    if not camera.running:
        camera.start(resolution_size(SETTINGS["resolution"]))
    camera.apply_controls(SETTINGS)
    ok, err = camera.start_video(VIDEO_PATH)
    if not ok:
        return jsonify(error=err), 500
    return jsonify(recording=True, file=VIDEO_PATH.name)


@app.route("/record/stop", methods=["POST"])
def record_stop():
    camera.stop_video()
    size = VIDEO_PATH.stat().st_size if VIDEO_PATH.exists() else 0
    return jsonify(recording=False, size=size)


@app.route("/video")
def video():
    if not VIDEO_PATH.exists():
        return "No video yet", 404
    return send_file(
        VIDEO_PATH,
        mimetype="video/h264",
        as_attachment=True,
        download_name="camweb.h264",
        max_age=0,
    )


@app.route("/power", methods=["POST"])
def power():
    data = request.get_json(silent=True) or request.form
    action = str(data.get("action", "")).strip().lower()
    if action not in ("poweroff", "reboot"):
        return jsonify(error="invalid action"), 400
    try:
        r = subprocess.run(
            ["sudo", "-n", "systemctl", action],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        return jsonify(error=str(e)), 500
    if r.returncode != 0:
        return jsonify(error=(r.stderr or "command failed").strip()), 500
    return jsonify(status=action)


# Start the camera and the timelapse worker once everything is defined, so that
# a reboot resumes an interrupted timelapse from state.json.
camera_start()
timelapse_load()
threading.Thread(target=timelapse_worker, daemon=True).start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False, threaded=True)
