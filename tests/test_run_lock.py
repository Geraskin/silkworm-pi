"""A run pins its exposure and white balance once, and every frame reuses them.

Two halves: what the camera is actually told (control mapping), and the
measure-then-hold lifecycle in the app. Neither needs a Pi.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_v2 as app
import camera as cam

# Both halves need the module to believe a camera is present.
real_available = cam.AVAILABLE
cam.AVAILABLE = True

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


# ---------------------------------------------------------------- camera side
class FakeEnum:
    def __getattr__(self, name):
        return f"<{name}>"


class FakeLc:
    """Stands in for libcamera so the AWB block really runs.

    Worth having: that block sets AwbEnable to True, and a lock that is applied
    before it instead of after would be quietly undone.
    """
    AwbModeEnum = FakeEnum()
    AeMeteringModeEnum = FakeEnum()
    AeExposureModeEnum = FakeEnum()


class FakePicam:
    def __init__(self):
        self.camera_controls = None
        self.controls = None

    def set_controls(self, controls):
        self.controls = controls


def bare_camera():
    """A Camera with no picamera2 behind it, only the parts apply_controls uses."""
    c = cam.Camera.__new__(cam.Camera)
    c._running = True
    c._picam = FakePicam()
    c._last_settings = {}
    return c


LOCK = {"exposure_us": 12345, "gain": 2.5, "colour_gains": [1.4, 1.9]}

real_lc = cam.lc
cam.lc = FakeLc()
try:
    c = bare_camera()
    c.apply_controls({"locked": LOCK})
    got = c._picam.controls
    check("the locked exposure reaches the camera",
          got.get("ExposureTime") == 12345, got.get("ExposureTime"))
    check("the locked gain reaches the camera",
          got.get("AnalogueGain") == 2.5, got.get("AnalogueGain"))
    check("the auto exposure is switched off",
          got.get("AeEnable") is False, got.get("AeEnable"))
    check("the auto white balance is switched off, not left running",
          got.get("AwbEnable") is False, got.get("AwbEnable"))
    check("the locked colour gains reach the camera",
          got.get("ColourGains") == (1.4, 1.9), got.get("ColourGains"))

    c = bare_camera()
    c.apply_controls({})
    got = c._picam.controls
    check("without a lock the auto exposure stays on",
          got.get("AeEnable") is True, got.get("AeEnable"))
    check("without a lock the auto white balance stays on",
          got.get("AwbEnable") is True, got.get("AwbEnable"))
    check("without a lock no exposure is forced",
          "ExposureTime" not in got, got.get("ExposureTime"))

    c = bare_camera()
    c.apply_controls({"locked": {"exposure_us": 0, "gain": 0}})
    got = c._picam.controls
    check("a half-measured lock sets no colour gains",
          "ColourGains" not in got, got.get("ColourGains"))
finally:
    cam.lc = real_lc

# ------------------------------------------------------------------ app side
app.nas_flush = lambda *a, **k: None
app.nas_prune = lambda *a, **k: None

tmp = Path(tempfile.mkdtemp())
local, base = tmp / "local", tmp / "base"
for d in (local, base):
    d.mkdir()
app.TIMELAPSE_DIR = local
app.TL_STATE_PATH = local / "state.json"
app.BASE_DIR = base
app.save_settings = lambda: None
app.SETTINGS.update(light_flash=True, light_flash_brightness=1.0,
                    light_flash_lead_s=0, light_on=False, resolution="3280x2464",
                    rotation=0, manual_exposure=False)


class FakeCamera:
    """A camera that reports a measurement and records what it is told."""

    def __init__(self):
        self.running = True
        self.recording = False
        self.preview_enabled = True
        self.answer = dict(LOCK)
        self.calls = []

    def start(self, *a, **k):
        self.running = True
        self.calls.append("start")

    def stop(self):
        self.running = False
        self.calls.append("stop")

    def set_preview_enabled(self, enabled):
        self.preview_enabled = bool(enabled)
        return True

    def apply_controls(self, s):
        self.calls.append(("controls", dict(s)))

    def measure_lock(self, *a, **k):
        self.calls.append("measure")
        if isinstance(self.answer, Exception):
            raise self.answer
        return dict(self.answer)


real_cam = app.camera
real_gpio = app.GPIO_AVAILABLE
app.GPIO_AVAILABLE = True
lit = []
real_apply_light = app.apply_light

try:
    fake = FakeCamera()
    app.camera = fake
    app.apply_light = lambda on=None, brightness=None: lit.append(on)

    state = app.timelapse_start(60, 0)
    session = state["session"]
    (local / session).mkdir(parents=True, exist_ok=True)

    kept = app._tl_state["lock"]
    check("the run keeps what was measured",
          {k: kept.get(k) for k in LOCK} == LOCK, kept)
    check("the run records the light it was measured in",
          kept.get("lamp_brightness") == 1.0 and kept.get("lamp_on") is True,
          kept)
    check("and says the values were measured, not typed",
          kept.get("chosen_by") == "measured", kept)
    check("the status says the run is locked", state["locked"] is True, state["locked"])
    check("the measurement happens before the run is marked active",
          fake.calls.index("measure") < len(fake.calls), fake.calls)
    check("the lamp is on while the measurement is taken",
          lit and lit[0] is True, lit)
    check("the lamp goes back to the user's state after the measurement",
          len(lit) > 1 and lit[-2] is None, lit)
    # ...and then the run itself goes dark: a lamp left burning would stand in
    # every frame and heat the box. A run is lit per frame, not for its length.
    check("the run starts with the lamp dark",
          lit[-1] is False, lit)

    locked = app.effective_controls().get("locked")
    check("every frame is shot with the measured values",
          {k: (locked or {}).get(k) for k in LOCK} == LOCK, locked)

    app.timelapse_write_meta(session)
    meta = json.loads((local / session / "session.json").read_text())
    check("the record keeps the exposure the run used",
          meta["locked_exposure_us"] == 12345, meta.get("locked_exposure_us"))
    check("the record keeps the gain the run used",
          meta["locked_gain"] == 2.5, meta.get("locked_gain"))
    check("the record keeps the colour gains the run used",
          meta["locked_colour_gains"] == [1.4, 1.9], meta.get("locked_colour_gains"))

    stopped = app.timelapse_stop()
    check("stopping takes the lock off the camera",
          "locked" not in app.effective_controls(),
          app.effective_controls().get("locked"))
    check("the values stay in the state for the record",
          app._tl_state["lock"].get("exposure_us") == LOCK["exposure_us"],
          app._tl_state.get("lock"))
    check("the status stops claiming a lock", stopped["locked"] is False,
          stopped["locked"])
    check("the camera gets the scene back after the run",
          "locked" not in app.effective_controls(), app.effective_controls().get("locked"))
    check("the camera is told about it",
          fake.calls[-1][0] == "controls" and "locked" not in fake.calls[-1][1],
          fake.calls[-1])

    # A camera that cannot be measured must not stop the run happening.
    lit.clear()
    fake.answer = RuntimeError("no camera today")
    state = app.timelapse_start(60, 0)
    (local / state["session"]).mkdir(parents=True, exist_ok=True)
    check("a failed measurement still lets the run start",
          state["active"] is True, state["active"])
    check("a failed measurement leaves the run unlocked",
          state["locked"] is False, state["locked"])
    check("an unlocked run has no lock in its controls",
          "locked" not in app.effective_controls())
    check("a failed measurement still leaves the lamp off, not lit",
          lit and lit[-1] is False, lit)
    app.timelapse_stop()

    # A camera that answers with nothing useful is the same as a failure.
    fake.answer = {}
    state = app.timelapse_start(60, 0)
    check("an empty measurement is not treated as a lock",
          state["locked"] is False, state["locked"])
    app.timelapse_stop()

    # A number the user typed is not the run's to change.
    app.SETTINGS.update(manual_exposure=True, shutter=25000, gain=4.0)
    fake.answer = dict(LOCK)
    state = app.timelapse_start(60, 0)
    (local / state["session"]).mkdir(parents=True, exist_ok=True)
    kept = app._tl_state["lock"]
    check("a hand-set shutter is used as it stands",
          kept.get("exposure_us") == 25000, kept)
    check("a hand-set gain is used as it stands", kept.get("gain") == 4.0, kept)
    check("and the record says the values were the user's",
          kept.get("chosen_by") == "hand", kept)
    check("the white balance is measured all the same",
          kept.get("colour_gains") == LOCK["colour_gains"], kept)
    app.timelapse_stop()
    app.SETTINGS.update(manual_exposure=False, shutter=10000, gain=1.0)
finally:
    app.camera = real_cam
    app.cam.AVAILABLE = real_available
    app.GPIO_AVAILABLE = real_gpio
    app.apply_light = real_apply_light
    shutil.rmtree(tmp, ignore_errors=True)

width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {extra if not ok else ''}")
    failed += not ok
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
