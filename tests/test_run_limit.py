"""A run can be given a length, and ends itself when the length is up.

Driven off a fake clock, so a three-day run is over in milliseconds and the
timing is exact rather than approximate.
"""
import json
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
local, base = tmp / "local", tmp / "base"
for d in (local, base):
    d.mkdir()
app.TIMELAPSE_DIR = local
app.TL_STATE_PATH = local / "state.json"
app.BASE_DIR = base
app.SETTINGS.update(resolution="3280x2464", rotation=0, manual_exposure=False,
                    light_flash=False)

clock = {"now": 100000.0}
real_time = time.time
time.time = lambda: clock["now"]

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


def start(interval_s, max_s):
    """Start a run and park the next frame far away, so ticks only test the limit."""
    state = app.timelapse_start(interval_s, max_s)
    (local / state["session"]).mkdir(parents=True, exist_ok=True)
    with app._tl_lock:
        app._tl_state["next_shot_at"] = clock["now"] + 1e9
    return state


try:
    # ------------------------------------------------------------ a limited run
    state = start(60, 3600)
    session = state["session"]
    check("the run keeps the length it was given", state["max_s"] == 3600,
          state["max_s"])
    check("a limited run reports the time left", state["remaining_s"] == 3600,
          state["remaining_s"])

    clock["now"] += 3599
    app.timelapse_tick()
    check("a run is not stopped a second early", app._tl_state["active"] is True,
          app.timelapse_status())
    check("the time left counts down", app.timelapse_remaining() == 1.0,
          app.timelapse_remaining())

    clock["now"] += 1
    app.timelapse_tick()
    check("the run stops itself at the limit", app._tl_state["active"] is False,
          app.timelapse_status())
    check("the state says why it stopped",
          "limit" in app._tl_state.get("stop_reason", ""),
          app._tl_state.get("stop_reason"))
    check("a run that is over reports no time left",
          app.timelapse_status()["remaining_s"] is None,
          app.timelapse_status()["remaining_s"])

    meta = json.loads((local / session / "session.json").read_text())
    check("the record keeps the length the run was given", meta["max_s"] == 3600,
          meta["max_s"])
    check("the record says why the run ended",
          "limit" in meta.get("stop_reason", ""), meta.get("stop_reason"))

    # ---------------------------------------------------------- an open-ended run
    clock["now"] += 10_000_000
    state = start(60, 0)
    check("a run without a length reports no end", state["remaining_s"] is None,
          state["remaining_s"])
    for _ in range(5):
        clock["now"] += 86400
        app.timelapse_tick()
    check("a run without a length is never stopped by the clock",
          app._tl_state["active"] is True, app.timelapse_status())
    check("a manual stop does not blame the clock",
          app.timelapse_stop()["stop_reason"] == "",
          app._tl_state.get("stop_reason"))

    # ------------------------------------------- resuming a run that was interrupted
    stamp = "2026-09-23T12:00:00"
    epoch = time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
    clock["now"] = epoch + 600
    app.TL_STATE_PATH.write_text(json.dumps({
        "active": True,
        "session": "tl-20260923-120000",
        "started_at": stamp,
        "interval_s": 60,
        "max_s": 7200,
        "frames": 3,
        "next_shot_at": clock["now"] + 1e9,
    }))
    app.timelapse_load()
    check("a state file from before this field existed is understood",
          app._tl_state.get("started_at_epoch") == epoch,
          app._tl_state.get("started_at_epoch"))
    check("a resumed run keeps counting from when it began",
          abs(app.timelapse_remaining() - 6600) < 1.5, app.timelapse_remaining())

    clock["now"] = epoch + 7201
    app.timelapse_tick()
    check("a resumed run ends at its own limit too",
          app._tl_state["active"] is False, app.timelapse_status())

    # ---------------------------------------------------------------- start route
    client = app.app.test_client()
    reply = client.post("/timelapse/start",
                        data={"interval_s": "30", "max_s": "172800"}).get_json()
    check("the start route passes the length through", reply["max_s"] == 172800,
          reply["max_s"])
    again = client.post("/timelapse/start", data={"interval_s": "30"})
    check("a second run cannot be started over a live one",
          again.status_code == 409, again.status_code)
    client.post("/timelapse/stop")
    reply = client.post("/timelapse/start", data={"interval_s": "30",
                                                  "max_s": "-5"}).get_json()
    check("a negative length means no limit", reply["max_s"] == 0.0, reply["max_s"])
    client.post("/timelapse/stop")
finally:
    time.time = real_time
    shutil.rmtree(tmp, ignore_errors=True)

width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {extra if not ok else ''}")
    failed += not ok
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
