"""The lamp is borrowed for the frame, not switched on for the run.

Nothing here needs a Pi: the driver is replaced by stubs that record the order
and the values of the writes, which is all the logic to check.
"""
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_v2 as app

# The worker thread starts on import; keep it away from the NAS while we test.
app.nas_flush = lambda *a, **k: None
app.nas_prune = lambda *a, **k: None

# A run writes state.json; keep that out of the real ~/camweb while we test.
_tmp = Path(tempfile.mkdtemp())
app.TIMELAPSE_DIR = _tmp / "timelapse"
app.TL_STATE_PATH = app.TIMELAPSE_DIR / "state.json"


class FakePin:
    def __init__(self):
        self.value = None
        self.calls = []

    def on(self):
        self.value = 1
        self.calls.append("on")

    def off(self):
        self.value = 0
        self.calls.append("off")


class FakePWM:
    def __init__(self):
        self.value = None
        self.frequency = 1000

    def off(self):
        self.value = 0


# Off the Pi the module switches the lamp off altogether.
app.GPIO_AVAILABLE = True
app.LIGHT_AIN1, app.LIGHT_AIN2 = FakePin(), FakePin()
app.LIGHT_STBY, app.LIGHT_PWM = FakePin(), FakePWM()

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


def reset():
    """Forget the last applied state, so the next call really writes."""
    app._LIGHT_STATE.update({"on": None, "duty_pct": None, "freq": None})
    app.LIGHT_PWM.value = None
    app.LIGHT_STBY.calls.clear()


try:
    # ------------------------------------------------ nothing lit, nothing touched
    app.SETTINGS.update(light_flash=False, light_on=False, light_brightness=0.5,
                        light_freq=1000)
    reset()
    with app.light_for_shot():
        lit = app.LIGHT_PWM.value
    check("the lamp is left alone when the flash is off", lit is None, lit)

    # ------------------------------------------------------- borrowed for the shot
    app.SETTINGS.update(light_flash=True, light_flash_brightness=1.0,
                        light_flash_lead_s=0.25, light_on=False, light_brightness=0.5)
    reset()
    started = time.time()
    with app.light_for_shot():
        lit = app.LIGHT_PWM.value
        waited = time.time() - started
    check("the lamp is on at the flash brightness while the frame is taken",
          lit == 1.0, lit)
    check("the lamp is off again once the frame is taken",
          app.LIGHT_PWM.value == 0.0, app.LIGHT_PWM.value)
    check("the lead time is waited out before the capture",
          waited >= 0.2, round(waited, 3))
    check("the lead time is not waited out twice",
          waited < 0.9, round(waited, 3))

    # ------------------------------------------- the user's own setting comes back
    app.SETTINGS.update(light_on=True, light_brightness=0.4, light_flash=True,
                        light_flash_brightness=1.0, light_flash_lead_s=0)
    reset()
    with app.light_for_shot():
        lit = app.LIGHT_PWM.value
    check("the flash brightness wins over the one the user chose", lit == 1.0, lit)
    check("the user's brightness is back afterwards",
          app.LIGHT_PWM.value == 0.4, app.LIGHT_PWM.value)

    # ------------------------------------------------- a failed frame still restores
    app.SETTINGS.update(light_on=False, light_brightness=0.5, light_flash=True,
                        light_flash_brightness=1.0, light_flash_lead_s=0)
    reset()
    try:
        with app.light_for_shot():
            raise RuntimeError("the capture blew up")
    except RuntimeError:
        pass
    check("the lamp goes off even when the capture fails",
          app.LIGHT_PWM.value == 0.0, app.LIGHT_PWM.value)

    # ------------------------------------------------- a run keeps the lamp dark
    # A lamp switched on by hand must not burn through a run: it would stand in
    # every frame as a light source and a shadow, cook the subject, and - in a
    # closed box - heat the lamp and the camera until the colour drifts.
    app.SETTINGS.update(light_on=True, light_brightness=0.5, light_flash=True,
                        light_flash_brightness=1.0, light_flash_lead_s=0)
    with app._tl_lock:
        app._tl_state["active"] = True
    reset()
    with app.light_for_shot():
        lit = app.LIGHT_PWM.value
    check("the lamp is lit for the frame of a run", lit == 1.0, lit)
    check("...and goes out in the gap after it, switch or no switch",
          app.LIGHT_PWM.value == 0.0, app.LIGHT_PWM.value)

    with app._tl_lock:
        app._tl_state["active"] = False
    reset()
    with app.light_for_shot():
        pass
    check("outside a run the switch means what it says",
          app.LIGHT_PWM.value == 0.5, app.LIGHT_PWM.value)

    # The run itself goes dark when it starts and hands the lamp back at the end.
    app.SETTINGS.update(light_on=True, light_brightness=0.5, light_flash=True,
                        light_flash_brightness=1.0, light_flash_lead_s=0,
                        manual_exposure=False)
    reset()
    app.timelapse_start(600, 0)
    with app._tl_lock:                     # park the first frame far away
        app._tl_state["next_shot_at"] = time.time() + 1e9
    check("a run starts with the lamp dark", app.LIGHT_PWM.value == 0.0,
          app.LIGHT_PWM.value)
    app.timelapse_stop()
    check("...and the switch takes the lamp back once the run ends",
          app.LIGHT_PWM.value == 0.5, app.LIGHT_PWM.value)

    app.SETTINGS.update(light_flash=False, light_on=True, light_brightness=0.5)
    reset()
    app.timelapse_start(600, 0)
    with app._tl_lock:
        app._tl_state["next_shot_at"] = time.time() + 1e9
    check("a run without the flash leaves the lamp where the user put it",
          app.LIGHT_PWM.value is None, app.LIGHT_PWM.value)
    app.timelapse_stop()

    # ------------------------------------------------------------- what the form does
    html = app.app.test_client().get("/").data.decode()
    for field in ("light_flash", "light_flash_brightness", "light_flash_lead_s"):
        m = re.search(r'<input[^>]*name="%s"[^>]*>' % re.escape(field), html)
        check(f"{field} belongs to the capture form",
              bool(m) and 'form="capture_form"' in m.group(0),
              (m.group(0) if m else "input not found"))

    # Saving settings must not touch the camera or write a file here.
    app.save_settings = lambda: None
    app.camera.reconfigure = lambda *a, **k: None
    app.camera.apply_controls = lambda *a, **k: None
    app.camera.set_preview_enabled = lambda *a, **k: None

    with app.app.test_request_context("/controls", method="POST", data={
            "light_flash": "on", "light_flash_brightness": "75",
            "light_flash_lead_s": "2.5"}):
        app.read_form()
    check("the form turns the flash on", app.SETTINGS["light_flash"] is True,
          app.SETTINGS["light_flash"])
    check("the form stores the flash brightness as a fraction",
          app.SETTINGS["light_flash_brightness"] == 0.75,
          app.SETTINGS["light_flash_brightness"])
    check("the form stores the lead time",
          app.SETTINGS["light_flash_lead_s"] == 2.5,
          app.SETTINGS["light_flash_lead_s"])

    with app.app.test_request_context("/controls", method="POST", data={
            "light_flash_brightness": "500"}):
        app.read_form()
    check("a form without the box turns the flash off",
          app.SETTINGS["light_flash"] is False, app.SETTINGS["light_flash"])
    check("a brightness over 100 % is clamped",
          app.SETTINGS["light_flash_brightness"] == 1.0,
          app.SETTINGS["light_flash_brightness"])
    check("a missing lead time keeps the last one",
          app.SETTINGS["light_flash_lead_s"] == 2.5,
          app.SETTINGS["light_flash_lead_s"])
finally:
    app.GPIO_AVAILABLE = False
    shutil.rmtree(_tmp, ignore_errors=True)

width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {extra if not ok else ''}")
    failed += not ok
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
