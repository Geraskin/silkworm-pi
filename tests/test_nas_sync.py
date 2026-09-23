"""Automatic sweeps must keep up with a run, not just tidy up once at boot.

Everything here runs off a fake monotonic clock, so the whole thing is over in
milliseconds and the timings are exact rather than hopeful.

The case that matters: the first sweep after start-up uploads the backlog, and
from then on anything new has to be picked up on its own. It was not - the next
pass looked after by pushing its own deadline forward, so it never arrived.
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_v2 as app

real_flush = app.nas_flush
app.nas_flush = lambda *a, **k: None          # keep the worker out of the way
app.nas_prune = lambda *a, **k: None

tmp = Path(tempfile.mkdtemp())
local, base, share = tmp / "local", tmp / "base", Path("/dev/shm/autosync_probe")
for d in (local, base):
    d.mkdir()
shutil.rmtree(share, ignore_errors=True)
share.mkdir(parents=True)

app.TIMELAPSE_DIR = local
app.BASE_DIR = base
app.SETTINGS.update(nas_enabled=True, nas_dir=str(share), nas_min_free_percent=10)
app.SETTINGS.update(manual_exposure=False)

clock = {"now": 1000.0}
real_monotonic = time.monotonic
time.monotonic = lambda: clock["now"]

fails = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {extra if not cond else ''}")
    fails.append(not cond)


def tick(seconds=1.0):
    """One pass of the background worker, one second later."""
    clock["now"] += seconds
    return real_flush()


def frames_on_nas(session):
    folder = share / time.strftime("%Y-%m-%d") / session
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir())


def make_session(name, count):
    d = local / name
    d.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (d / f"frame_{i + 1:06d}.jpg").write_bytes(b"x" * 100)
    return d


try:
    app._nas_next_try = 0.0
    app._nas_mirrored.clear()
    app._nas_pending = None

    # A quiet start: the pass finds nothing and settles into its interval.
    tick(0.0)
    tick()
    check("an empty cache uploads nothing", frames_on_nas("tl-20260923-130000") == [])

    # A run starts. Nothing forces the next pass - it has to come on its own.
    name = "tl-20260923-130000"
    make_session(name, 2)
    for _ in range(20):
        tick()
        if len(frames_on_nas(name)) >= 2:
            break
    check("a session created while running is uploaded by itself",
          len(frames_on_nas(name)) == 2, frames_on_nas(name))

    # The queue has to say so while the frames are still waiting.
    app._nas_mirrored.clear()
    app._nas_pending = None
    make_session(name, 3)               # a third frame appears
    app._nas_forget(name)
    app._nas_note_frame()
    waiting = app.nas_pending()
    check("a waiting frame is counted", waiting is not None and waiting > 0, waiting)

    for _ in range(20):
        tick()
        if app.nas_pending() == 0:
            break
    check("the count falls back to zero once it is up",
          app.nas_pending() == 0 and len(frames_on_nas(name)) == 3,
          (app.nas_pending(), frames_on_nas(name)))

    # Stopping rewrites the record. The NAS copy must follow, without forcing.
    with app._tl_lock:
        app._tl_state["session"] = name
        app._tl_state["ended_at"] = "2026-09-23T13:05:00"
        app._tl_state["frames"] = 3
    app.timelapse_write_meta()
    for _ in range(20):
        tick()
        record = share / time.strftime("%Y-%m-%d") / name / "session.json"
        if record.is_file():
            break
    record = share / time.strftime("%Y-%m-%d") / name / "session.json"
    check("the record reaches the NAS after a stop",
          record.is_file() and json.loads(record.read_text())["ended_at"]
          == "2026-09-23T13:05:00",
          record.read_text() if record.is_file() else "missing")

    # A frame landing while a pass is listing the folder must not be left behind:
    # that pass cannot vouch for a session it only half saw.
    other = "tl-20260923-140000"
    make_session(other, 1)
    app._nas_mirrored.clear()
    real_listing = app._nas_listing
    state = {"once": False}

    def listing_then_write(session_dir):
        if not state["once"] and session_dir.name == other:
            state["once"] = True
            (local / other / "frame_000002.jpg").write_bytes(b"x" * 100)
            app._nas_forget(other)                  # a shot lands mid-pass
            app._nas_note_frame()
        return real_listing(session_dir)

    app._nas_listing = listing_then_write
    try:
        real_flush(force=True)
    finally:
        app._nas_listing = real_listing
    check("a session written to during a pass is not marked complete",
          other not in app._nas_mirrored, app._nas_mirrored)
    for _ in range(20):
        tick()
        if len(frames_on_nas(other)) == 2:
            break
    check("the frame that landed mid-pass still gets uploaded",
          len(frames_on_nas(other)) == 2, frames_on_nas(other))

finally:
    time.monotonic = real_monotonic
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(share, ignore_errors=True)

print(f"\n{sum(not f for f in fails)}/{len(fails)} passed")
raise SystemExit(1 if any(fails) else 0)
