"""One browser drives the camera, and the camera is only up while one is watching.

The lease is driven off a fake clock, because everything interesting about it is
about time: when it is handed over, when it is taken back, and when the camera is
released. The HTTP side is checked with two test clients, which is as close as a
test can get to two browsers.
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_v2 as app

app.nas_flush = lambda *a, **k: None
app.nas_prune = lambda *a, **k: None

tmp = Path(tempfile.mkdtemp())
local = tmp / "local"
local.mkdir()
app.TIMELAPSE_DIR = local
app.TL_STATE_PATH = local / "state.json"
app.save_settings = lambda: None

clock = {"now": 5000.0}
real_monotonic = time.monotonic
time.monotonic = lambda: clock["now"]

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


def fresh():
    """Forget every page and the camera, as at start-up."""
    with app._watch_lock:
        app._watchers.clear()
        app._controller = ""
    app._camera_wanted_at = 0.0
    clock["now"] += 1000.0


class FakeCamera:
    def __init__(self):
        self.running = True
        self.recording = False
        self.preview_enabled = True
        self.preview_blocked_until = 0.0
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
        self.calls.append("controls")

    def frames(self, keep_alive=None):
        return iter(())


real_camera = app.camera
real_available = app.cam.AVAILABLE
app.cam.AVAILABLE = True          # off the Pi the module reports no camera
app.camera = FakeCamera()
app.SETTINGS.update(preview_enabled=True, focus_mode=False)

try:
    # ------------------------------------------------------------------- the lease
    fresh()
    check("the first page to ask gets the lease", app.watch_beat("a") is True)
    check("a second page does not", app.watch_beat("b") is False)
    check("both pages count as watching", app.anyone_watching() is True)

    check("a page that keeps asking keeps the lease",
          app.watch_beat("a") is True)
    clock["now"] += app.WATCHER_TIMEOUT_S + 1
    check("a page that stops asking stops counting",
          app.anyone_watching() is False)
    check("and the lease falls to whoever is still asking",
          app.watch_beat("b") is True)

    clock["now"] += 1
    check("a page can be taken over from", app.watch_beat("a", take=True) is True)
    check("the page it was taken from loses it", app.watch_beat("b") is False)

    # --------------------------------------------------------- the camera's life
    fresh()
    app.camera = FakeCamera()
    app._camera_wanted_at = clock["now"]
    app.camera_idle_check()
    check("the camera is not dropped the moment it is wanted",
          app.camera.running is True, app.camera.calls)

    clock["now"] += app.CAMERA_IDLE_S + 1
    app.camera_idle_check()
    check("the camera is released once nothing wants it",
          app.camera.running is False, app.camera.calls)
    check("releasing it goes through the full teardown",
          app.camera.calls == ["stop"], app.camera.calls)

    app.camera = FakeCamera()
    app._camera_wanted_at = clock["now"]
    app.watch_beat("a")
    clock["now"] += 5.0
    app.camera_idle_check()
    check("a browser that is still watching keeps it", app.camera.running is True,
          app.camera.calls)
    check("and watching is what makes it needed", app.camera_needed() is True)

    clock["now"] += app.WATCHER_TIMEOUT_S + app.CAMERA_IDLE_S + 1
    app.camera_idle_check()
    check("once nobody is watching, the camera goes too",
          app.camera.running is False, app.camera.calls)

    # ------------------------------------------------------------- focus mode off
    app.SETTINGS["focus_mode"] = True
    app.camera.preview_blocked_until = 999.0
    check("focus mode can be left", app.focus_mode_off("test") is True)
    check("leaving it clears the setting", app.SETTINGS["focus_mode"] is False)
    check("leaving it frees the preview at once",
          app.camera.preview_blocked_until == 0.0, app.camera.preview_blocked_until)
    check("leaving it twice is harmless", app.focus_mode_off("test") is False)

    # ------------------------------------------------- one browser, over real HTTP
    fresh()
    app.SETTINGS.update(preview_enabled=True, focus_mode=False)
    away = {"REMOTE_ADDR": "192.168.10.50"}
    first = app.app.test_client()
    second = app.app.test_client()

    first.get("/", environ_base=away)          # mints the page id
    second.get("/", environ_base=away)
    check("a page starts with no lease",
          first.get("/alive", environ_base=away).get_json()["in_charge"] is True,
          "first page should have it")
    check("the second page is told it does not",
          second.get("/alive", environ_base=away).get_json()["in_charge"] is False)

    reply = first.post("/light", json={"on": False}, environ_base=away)
    check("the page holding the lease may drive the camera",
          reply.status_code == 200, reply.status_code)
    reply = second.post("/light", json={"on": False}, environ_base=away)
    check("a read-only page is refused",
          reply.status_code == 409, reply.status_code)
    check("and is told why",
          "camera" in (reply.get_json() or {}).get("error", ""), reply.get_json())

    reply = second.get("/stream", environ_base=away)
    check("a read-only page is not given the preview",
          reply.status_code == 204, reply.status_code)
    check("the page in charge is given it",
          first.get("/stream", environ_base=away).status_code == 200)

    reply = second.get("/alive?take=1", environ_base=away)
    check("a page can take the camera over",
          reply.get_json()["in_charge"] is True, reply.get_json())
    check("after which the other one is refused",
          first.post("/light", json={"on": False},
                     environ_base=away).status_code == 409)

    # A script on the Pi is not a second browser.
    nearby = {"REMOTE_ADDR": "127.0.0.1"}
    check("loopback is never refused",
          app.app.test_client().post("/light", json={"on": False},
                                     environ_base=nearby).status_code == 200)
    check("reading the status is always allowed",
          second.get("/timelapse/state", environ_base=away).status_code == 200)
finally:
    time.monotonic = real_monotonic
    app.camera = real_camera
    app.cam.AVAILABLE = real_available
    shutil.rmtree(tmp, ignore_errors=True)

width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {extra if not ok else ''}")
    failed += not ok
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
