"""The numbers the run panel shows: frames taken, frames to go, room left.

The plan is worked out from the schedule rather than from a clock, so a run of
three days is checked here in the time it takes to call a function. What this
guards is the figure a person watching that run reads twice - and the "frames
412" a page can show about a folder that is no longer there.
"""
import json
import os
import re
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
# The import above read the real state.json, before the paths were redirected to
# the empty folder - so read it again from there. Otherwise a run left behind on
# the machine this is run on leaks into the numbers below: `last_shot_seconds`
# alone would shift the plan by a frame.
app.timelapse_load()
app.SETTINGS.update(resolution="3280x2464", rotation=0, manual_exposure=False,
                    light_flash=False, light_on=False, nas_enabled=False,
                    quality=93, denoise="fast", awb="auto", metering="centre",
                    nas_min_free_percent=10.0)

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


# Every key session.json carries. Compared as a set and not as a subset: a key
# that quietly stopped being written is the kind of thing only a folder opened
# months later would notice - and most of them would not even raise on the way.
EXPECTED_META = {
    "session", "started_at", "ended_at", "frames", "interval_s", "resolution",
    "save_raw", "quality", "rotation", "hflip", "vflip", "exposure_mode",
    "shutter_us", "gain", "denoise", "awb", "metering", "sharpness",
    "light_on", "light_brightness", "light_flash", "light_flash_brightness",
    "light_flash_lead_s", "max_s", "stop_reason", "locked_exposure_us",
    "locked_gain", "locked_colour_gains", "locked_chosen_by",
    "locked_lamp_brightness", "locked_gain_limited",
}


def plan(**fields):
    """A plan for a run that started at t=1000 and was read at t=1010."""
    state = {"interval_s": 60.0, "max_s": 0.0, "frames": 0, "active": True,
             "started_at_epoch": 1000.0, "next_shot_at": 1060.0}
    state.update(fields)
    return app.run_plan(state, now=1010.0)


try:
    # ------------------------------------------------------------- the count
    check("an hour every minute is 60 frames", plan(max_s=3600)["planned"] == 60,
          plan(max_s=3600)["planned"])
    check("ten minutes every minute is 10 frames", plan(max_s=600)["planned"] == 10,
          plan(max_s=600)["planned"])
    check("the frame that would land on the limit is not counted",
          plan(max_s=599)["planned"] == 10, plan(max_s=599)["planned"])
    check("just past one interval is a second frame", plan(max_s=61)["planned"] == 2,
          plan(max_s=61)["planned"])
    check("a limit of exactly one interval is one frame",
          plan(max_s=60)["planned"] == 1, plan(max_s=60)["planned"])
    check("a limit shorter than the interval is still one frame",
          plan(max_s=1)["planned"] == 1, plan(max_s=1)["planned"])
    check("a day every hour is 24 frames",
          plan(interval_s=3600, max_s=86400)["planned"] == 24,
          plan(interval_s=3600, max_s=86400)["planned"])

    # ------------------------------------------------------------ what is left
    check("frames already taken come off the plan",
          plan(max_s=3600, frames=12)["left"] == 48,
          plan(max_s=3600, frames=12)["left"])
    check("a plan that is used up leaves nothing",
          plan(max_s=3600, frames=60)["left"] == 0)
    check("the plan never sits below what is already taken",
          plan(max_s=3600, frames=99)["left"] == 0,
          plan(max_s=3600, frames=99)["left"])
    check("the bar is the share of the plan done",
          abs(plan(max_s=3600, frames=12)["done_fraction"] - 0.2) < 1e-9,
          plan(max_s=3600, frames=12)["done_fraction"])

    # ------------------------------------------------------ what a frame costs
    # A frame is not free: the lamp lead and the capture take time, and the next
    # frame is counted from the end of the previous one. A five-second test whose
    # frames cost four seconds really holds three frames, not four - which is
    # exactly what a plan built on the interval alone would promise.
    check("a frame that costs time stretches the pace",
          plan(interval_s=5, max_s=20, last_shot_seconds=4)["planned"] == 3,
          plan(interval_s=5, max_s=20, last_shot_seconds=4)["planned"])
    check("before the first frame the plan can only assume the interval",
          plan(interval_s=5, max_s=20)["planned"] == 4,
          plan(interval_s=5, max_s=20)["planned"])
    check("a long run loses the frames the shot time eats",
          plan(max_s=3600, last_shot_seconds=4)["planned"] == 57,
          plan(max_s=3600, last_shot_seconds=4)["planned"])
    check("the plan never promises fewer frames than are taken",
          plan(interval_s=5, max_s=20, last_shot_seconds=4,
               frames=5)["planned"] == 5,
          plan(interval_s=5, max_s=20, last_shot_seconds=4, frames=5)["planned"])
    check("a run that has taken all it can shows nothing left",
          plan(interval_s=5, max_s=20, last_shot_seconds=4, frames=3)["left"] == 0)

    # ----------------------------------------------------------- without a limit
    unlimited = plan(max_s=0, frames=7)
    check("with no limit the total is unknown rather than invented",
          unlimited["planned"] is None and unlimited["left"] is None, unlimited)
    check("...and the bar is unknown with it",
          unlimited["done_fraction"] is None, unlimited["done_fraction"])

    # ------------------------------------------------------------------ timing
    timed = plan(max_s=3600, frames=12)
    check("elapsed comes from the start, not the last frame",
          timed["elapsed_s"] == 10, timed["elapsed_s"])
    check("time left counts down from the limit", timed["left_s"] == 3590,
          timed["left_s"])
    check("the next frame is the countdown to it", timed["next_s"] == 50,
          timed["next_s"])
    stopped = plan(active=False, frames=12, last_shot_at=1300.0, max_s=3600)
    check("a stopped run stops growing", stopped["elapsed_s"] == 300,
          stopped["elapsed_s"])
    check("...and has no time left and no next frame",
          stopped["left_s"] is None and stopped["next_s"] is None, stopped)
    check("...but its plan is still there to read",
          stopped["planned"] == 60 and stopped["left"] == 48, stopped)

    check("...and it is not computed from a start time nobody has",
          plan(max_s=3600, started_at_epoch=0)["left_s"] is None,
          plan(max_s=3600, started_at_epoch=0)["left_s"])

    # ------------------------------------------------- nonsense in the state file
    check("an interval of zero is read as one second",
          plan(interval_s=0)["interval_s"] == 1.0, plan(interval_s=0)["interval_s"])
    junk = plan(interval_s=None, max_s=None, frames="not a number",
                started_at_epoch=None)
    check("nonsense in the state does not crash the plan",
          junk["interval_s"] == 60.0 and junk["planned"] is None
          and junk["taken"] == 0 and junk["elapsed_s"] == 0, junk)

    # ---------------------------------------------------------- what it costs
    session = "tl-20260923-120000"
    folder = local / session
    folder.mkdir()
    for i in (1, 2, 3):
        (folder / f"frame_{i:06d}.jpg").write_bytes(b"x" * 1000)
    (folder / "frame_000004.tmp.jpg").write_bytes(b"x" * 5000)
    (folder / "session.json").write_text("{}", encoding="utf-8")
    check("the size counts the frames and nothing else",
          app._session_bytes(session, 3) == 3000, app._session_bytes(session, 3))
    (folder / "frame_000004.jpg").write_bytes(b"x" * 1000)
    check("a new frame is a recount, not a guess",
          app._session_bytes(session, 4) == 4000, app._session_bytes(session, 4))
    app._SESSION_BYTES[session] = (4, 999, 1)
    check("a stale entry is replaced once the count moves on",
          app._session_bytes(session, 4) == 4000, app._session_bytes(session, 4))
    # A frame taken away and another added between two polls: the count is where
    # it was, so only the folder's own timestamp says the total has moved. The
    # timestamp is set forward by hand rather than hoped for, because a coarse
    # filesystem can put both writes inside the same second.
    (folder / "frame_000004.jpg").unlink()
    (folder / "frame_000005.jpg").write_bytes(b"y" * 2000)
    stamp = folder.stat().st_mtime_ns + 5_000_000_000
    os.utime(folder, ns=(stamp, stamp))
    check("a swap that leaves the count alone is still a recount",
          app._session_bytes(session, 4) == 5000, app._session_bytes(session, 4))
    # Put the folder back the way the payload below expects to find it: four
    # frames of a thousand bytes each.
    (folder / "frame_000005.jpg").unlink()
    (folder / "frame_000004.jpg").write_bytes(b"x" * 1000)
    check("a session that is not there has no size",
          app._session_bytes("tl-20260101-000000", 0) == 0)
    check("a name that is not one of ours has no size",
          app._session_bytes("../../etc", 0) == 0)

    # ------------------------------------------------------------- the payload
    with app._tl_lock:
        app._tl_state.update({
            "active": True, "session": session, "frames": 4, "interval_s": 60.0,
            "max_s": 3600.0, "started_at": "2026-09-23T12:00:00",
            "started_at_epoch": time.time() - 10, "next_shot_at": time.time() + 50,
            "save_raw": False,
            "lock": {"exposure_us": 10000, "gain": 1.0,
                     "colour_gains": [1.9, 1.5], "chosen_by": "measured"},
        })
    status = app.timelapse_status()
    check("the page is told how many frames are taken",
          status["plan"]["taken"] == 4, status["plan"])
    check("...and how many the run is for", status["plan"]["planned"] == 60,
          status["plan"]["planned"])
    check("...and how many are left", status["plan"]["left"] == 56,
          status["plan"]["left"])
    check("...and what the frames weigh", status["session_bytes"] == 4000,
          status["session_bytes"])
    check("...and what one frame costs", status["avg_frame_bytes"] == 1000,
          status["avg_frame_bytes"])
    check("...and what the whole run will cost",
          status["estimated_bytes"] == 60000, status["estimated_bytes"])
    check("...and how much room the card has", status["free_mb"] > 0,
          status["free_mb"])
    check("...and that the frames really are on the card",
          status["session_on_card"] is True)
    check("...and what the run is shooting with",
          status["shot_with"]["resolution"] == "3280x2464"
          and status["shot_with"]["interval_s"] == 60.0
          and status["shot_with"]["locked_exposure_us"] == 10000,
          status["shot_with"])
    check("the page and the NAS record describe the same run",
          status["shot_with"]["locked_colour_gains"] == [1.9, 1.5],
          status["shot_with"]["locked_colour_gains"])

    # What travels to the NAS is written from the same description, so it still
    # has to be the file it always was - a key dropped in the refactor would only
    # be noticed months later, in a folder nobody can ask about.
    app.timelapse_write_meta(session)
    written = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    check("the record of the run is still written next to the frames",
          written["session"] == session and written["frames"] == 4, written)
    check("...with everything a folder opened later has to explain itself with",
          set(written) == EXPECTED_META, sorted(set(written) ^ EXPECTED_META))
    check("...and it agrees with what the page is showing",
          written["locked_exposure_us"] == status["shot_with"]["locked_exposure_us"]
          and written["resolution"] == status["shot_with"]["resolution"])

    # ------------------------------------------------- frames gone from the card
    shutil.rmtree(folder, ignore_errors=True)
    gone = app.timelapse_status()
    check("a session whose folder is gone says so",
          gone["session_on_card"] is False)
    check("...and its size is zero rather than remembered",
          gone["session_bytes"] == 0, gone["session_bytes"])
    check("...and the rate is not computed from a folder that is not there",
          gone["avg_frame_bytes"] == 0 and gone["estimated_bytes"] is None,
          gone["avg_frame_bytes"])

    with app._tl_lock:
        app._tl_state.update({"active": False, "max_s": 0.0})
    idle = app.timelapse_status()
    check("a run with no limit reports no total and no estimate",
          idle["plan"]["planned"] is None and idle["estimated_bytes"] is None,
          idle["plan"])

    # -------------------------------------------------- a new run starts clean
    app.timelapse_start(600, 0)
    check("a new run does not inherit the last one's frame cost",
          app._tl_state.get("last_shot_seconds") == 0.0,
          app._tl_state.get("last_shot_seconds"))
    app.timelapse_stop()

    # ------------------------------------------------ and a failure is priced
    # A shot that fails costs the schedule the same time a successful one does,
    # so its cost is recorded as well - otherwise a run whose frames keep failing
    # goes on promising the pace of the last frame that worked.
    parked = "tl-20260923-130000"
    (local / parked).mkdir()
    with app._tl_lock:
        app._tl_state.update({"active": True, "session": parked, "frames": 0,
                              "interval_s": 60.0, "max_s": 0.0,
                              "started_at_epoch": time.time() - 1,
                              "next_shot_at": time.time() + 1e9,
                              "last_shot_at": 0.0, "last_shot_seconds": 9.9,
                              "save_raw": False})
    app.timelapse_shot()
    check("a frame that fails still has its cost recorded",
          app._tl_state.get("last_shot_seconds") != 9.9,
          app._tl_state.get("last_shot_seconds"))
    check("...and it is not counted as a frame", app._tl_state["frames"] == 0,
          app._tl_state["frames"])
    check("...and the run says why it did not work",
          bool(app._tl_state.get("last_error")), app._tl_state.get("last_error"))
    with app._tl_lock:
        app._tl_state["active"] = False

    # ---------------------------------------------------- the page it lives on
    # The panel is the one thing here a payload cannot describe, so at least
    # every element the script reaches for has to exist on the page.
    used = set(re.findall(r'getElementById\("([^"]+)"\)', app.HTML))
    defined = set(re.findall(r'id="([^"]+)"', app.HTML))
    check("every element the script reaches for is on the page",
          used <= defined, sorted(used - defined)[:8])
    check("the panel's own parts are among them",
          {"run_panel", "run_grid", "run_bar_fill", "run_last_img",
           "run_last_link", "run_last_meta", "run_earlier"} <= defined,
          sorted({"run_panel", "run_grid", "run_bar_fill", "run_last_img",
                  "run_last_link", "run_last_meta", "run_earlier"} - defined))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {extra if not ok else ''}")
    failed += not ok
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
