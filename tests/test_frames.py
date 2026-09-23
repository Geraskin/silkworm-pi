"""The shutter is typed, not chosen from a list, and the frames can be looked at.

The list the UI used to offer could not represent the values the camera actually
settles on - and the camera was configured with a plain frame rate, which capped
every exposure at the frame period without saying so.
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_v2 as app
import camera as cam

try:
    from PIL import Image
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

app.nas_flush = lambda *a, **k: None
app.nas_prune = lambda *a, **k: None
app.save_settings = lambda: None
app.camera.reconfigure = lambda *a, **k: None
app.camera.apply_controls = lambda *a, **k: None
app.camera.set_preview_enabled = lambda *a, **k: None

tmp = Path(tempfile.mkdtemp())
local = tmp / "local"
local.mkdir()
app.TIMELAPSE_DIR = local
app.TL_STATE_PATH = local / "state.json"
app.BASE_DIR = tmp / "base"
app.BASE_DIR.mkdir()

SESSION = "tl-20260923-120000"
frames = local / SESSION
frames.mkdir()
if HAVE_PIL:
    for i in (1, 2, 3):
        Image.new("RGB", (640, 480), (40 * i, 200, 90)).save(frames / f"frame_{i:06d}.jpg",
                                                            quality=80)
else:
    for i in (1, 2, 3):
        (frames / f"frame_{i:06d}.jpg").write_bytes(b"\xff\xd8\xff\xd9")
# A frame still being written must never turn up in the strip.
(frames / "frame_000004.tmp.jpg").write_bytes(b"\xff\xd8\xff\xd9")

app._tl_state.update(session=SESSION, active=True, interval_s=60.0, frames=3)
app.SETTINGS.update(manual_exposure=False)

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


try:
    # ------------------------------------------------------------ typed shutter
    check("the ceiling is the one the camera is given",
          app.SHUTTER_MAX_US == cam.SHUTTER_MAX_US, app.SHUTTER_MAX_US)
    check("it is long enough to be worth having", app.SHUTTER_MAX_US >= 1_000_000,
          app.SHUTTER_MAX_US)

    def saved_shutter(value):
        with app.app.test_request_context("/controls", method="POST",
                                          data={"shutter": str(value),
                                                "manual_exposure": "on"}):
            app.read_form()
        return app.SETTINGS["shutter"]

    check("a shutter the list never held is kept as typed",
          saved_shutter(7826) == 7826, saved_shutter(7826))
    check("a two second exposure is kept", saved_shutter(2000000) == 2000000,
          saved_shutter(2000000))
    check("anything longer is clamped to the ceiling",
          saved_shutter(50_000_000) == app.SHUTTER_MAX_US, saved_shutter(50_000_000))
    check("and something absurdly short is clamped up",
          saved_shutter(1) == 100, saved_shutter(1))

    html = app.app.test_client().get("/").data.decode()
    check("the shutter is a field, not a list",
          'name="shutter"' in html and "<select name=\"shutter\"" not in html)
    check("the field says what the ceiling is",
          ('max="%d"' % app.SHUTTER_MAX_US) in html)
    check("the usual fractions are still offered as shortcuts",
          'id="shutter_choices"' in html and "1000000" in html)

    # -------------------------------------------------------------- the frames
    client = app.app.test_client()
    listed = client.get("/timelapse/frames").get_json()
    check("the run view is told which session it is looking at",
          listed["session"] == SESSION, listed["session"])
    check("every finished frame is listed",
          [f["name"] for f in listed["frames"]] ==
          ["frame_000001.jpg", "frame_000002.jpg", "frame_000003.jpg"],
          [f["name"] for f in listed["frames"]])
    check("a frame still being written is not",
          all("tmp" not in f["name"] for f in listed["frames"]), listed["frames"])
    check("the list carries a size and a time",
          all(f["bytes"] > 0 and f["at"] for f in listed["frames"]), listed["frames"])

    whole = client.get(f"/timelapse/frame/{SESSION}/frame_000001.jpg")
    check("a frame can be fetched whole",
          whole.status_code == 200 and whole.data[:2] == b"\xff\xd8",
          (whole.status_code, whole.data[:4]))
    check("it is sent as a JPEG",
          whole.headers.get("Content-Type") == "image/jpeg",
          whole.headers.get("Content-Type"))

    if HAVE_PIL:
        small = client.get(f"/timelapse/frame/{SESSION}/frame_000001.jpg?w=64")
        check("a small copy can be had", small.status_code == 200, small.status_code)
        check("and it really is smaller",
              0 < len(small.data) < len(whole.data), (len(small.data), len(whole.data)))
        again = client.get(f"/timelapse/frame/{SESSION}/frame_000001.jpg?w=64")
        check("the small copy is kept, not remade", again.data == small.data)
    else:
        check("a small copy can be had (Pillow missing, skipped)", True, "")

    check("a session that is not ours is refused",
          client.get("/timelapse/frame/..%2F..%2Fetc/frame_000001.jpg").status_code == 404)
    check("a frame name that is not a frame is refused",
          client.get(f"/timelapse/frame/{SESSION}/session.json").status_code == 404)
    check("a frame that is not there is refused",
          client.get(f"/timelapse/frame/{SESSION}/frame_000099.jpg").status_code == 404)
    check("an unknown session is refused",
          client.get("/timelapse/frame/tl-20260923-999999/frame_000001.jpg").status_code == 404)

    # Reading what has been captured must not need the lease, or a page that lost
    # it would go blank in the middle of a run.
    away = {"REMOTE_ADDR": "192.168.10.50"}
    check("the frames can be read without holding the lease",
          client.get("/timelapse/frames", environ_base=away).status_code == 200)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {extra if not ok else ''}")
    failed += not ok
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
