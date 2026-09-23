"""What the NAS export has to get right, whatever the share does.

Runs entirely locally: no camera, no Pi, no privileges. For the filesystem check
a real mount is needed, so /dev/shm (a tmpfs that already exists in this
container) plays the part of the NAS, and ordinary folders play the part of a
path that exists but is not a share.
"""
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_v2 as app

# The module starts its worker thread on import; keep it out of the way.
real_flush, real_prune = app.nas_flush, app.nas_prune
app.nas_flush = lambda *a, **k: None
app.nas_prune = lambda *a, **k: None

NAS_ROOT = Path("/dev/shm")          # a real mount point
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


def make_session(root, name, files):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_bytes(b"x" * 1234)
    return d


def clean_shm():
    """Remove anything a previous run left in the fake NAS."""
    for p in NAS_ROOT.iterdir():
        if not p.is_dir():
            continue
        dated = (len(p.name) == 10 and p.name[4] == "-" and p.name[7] == "-")
        if p.name.startswith("tl-") or dated:
            shutil.rmtree(p, ignore_errors=True)


tmp = Path(tempfile.mkdtemp())
local, plain = tmp / "local", tmp / "plain"
for d in (local, plain):
    d.mkdir()

app.TIMELAPSE_DIR = local
free = {"pct": 50.0}
app.disk_free_percent = lambda: free["pct"]

try:
    # ---------------------------------------------------------------- finding 3
    app.SETTINGS.update(nas_enabled=True, nas_dir=str(plain), nas_min_free_percent=10)
    ready, why = app.nas_check()
    check("a folder on the same filesystem as the card is refused",
          not ready and "not on a mounted share" in why, why)
    r = real_flush(force=True)
    check("nothing is uploaded into an unmounted path",
          r["ok"] is False and not list(plain.rglob("*.*")), str(r))
    check("nothing is deleted for an unmounted path", real_prune(force=True) == 0)

    app.SETTINGS.update(nas_dir=str(NAS_ROOT))
    check("a real mount point is accepted", app.nas_check() == (True, ""),
          str(app.nas_check()))

    # the usual layout: a share mounted at /mnt/video_sources and the frames
    # going to a subfolder of it, which is on the share's filesystem but is not
    # itself a mount point
    sub_nas = NAS_ROOT / "timelaps"
    sub_nas.mkdir(exist_ok=True)
    app.SETTINGS.update(nas_dir=str(sub_nas))
    check("a subfolder of the mounted share is accepted",
          app.nas_check() == (True, ""), str(app.nas_check()))
    app.SETTINGS.update(nas_dir=str(NAS_ROOT))

    # the cache may not be its own archive
    saved_dir = app.TIMELAPSE_DIR
    app.TIMELAPSE_DIR = NAS_ROOT
    app.SETTINGS.update(nas_dir=str(NAS_ROOT))
    ready, why = app.nas_check()
    check("the cache pointed at itself is refused",
          not ready and "local cache" in why, why)
    app.TIMELAPSE_DIR = saved_dir

    # ---------------------------------------------------------------- finding 4
    app.SETTINGS.update(nas_dir=str(NAS_ROOT), nas_min_free_percent=10)
    app._nas_mirrored.clear()
    free["pct"] = 50.0
    clean_shm()

    s1 = make_session(local, "tl-20260918-100000", ["frame_000001.jpg", "frame_000002.dng"])
    r = real_flush(force=True)
    check("both frames are uploaded", r["ok"] and r["uploaded"] == 2, str(r))
    check("local copies are kept",
          (s1 / "frame_000001.jpg").exists() and (s1 / "frame_000002.dng").exists())
    r = real_flush(force=True)
    check("a second sweep uploads nothing", r["uploaded"] == 0, str(r))

    # ---------------------------------------------------------------- finding 2
    (s1 / "frame_000003.tmp.jpg").write_bytes(b"half a frame")
    check("a half-written frame is not counted as a frame",
          all(p.name != "frame_000003.tmp.jpg" for p in app._frame_files(s1)))
    real_flush(force=True)
    check("a half-written frame is never published",
          not (NAS_ROOT / "tl-20260918-100000" / "frame_000003.tmp.jpg").exists())
    (s1 / "frame_000003.tmp.jpg").unlink()

    # a fully archived session must not be re-listed on every sweep
    listed = []
    real_listing = app._nas_listing

    def counting(session_dir):
        listed.append(Path(session_dir).name)
        return real_listing(session_dir)

    app._nas_listing = counting

    app._nas_next_try = 0.0
    listed.clear()
    real_flush()
    check("an archived session is skipped on an ordinary sweep",
          "tl-20260918-100000" not in listed, str(listed))

    s2 = make_session(local, "tl-20260918-110000", ["frame_000001.jpg"])
    app._nas_next_try = 0.0
    listed.clear()
    r = real_flush()
    check("a new session is swept",
          "tl-20260918-110000" in listed and r["uploaded"] == 1, f"{listed} {r}")
    check("the archived session stays skipped",
          "tl-20260918-100000" not in listed, str(listed))

    listed.clear()
    real_flush(force=True)
    check("a forced sweep re-verifies everything",
          "tl-20260918-100000" in listed, str(listed))

    # an unreachable NAS is rescanned on a slow cadence, not every second
    app.SETTINGS.update(nas_dir=str(plain))
    app._nas_next_try = 0.0
    real_flush()
    counted = []
    real_count = app._local_file_count

    def counting_count():
        counted.append(1)
        return real_count()

    app._local_file_count = counting_count
    app._nas_next_try = time.monotonic() + 100.0
    real_flush()
    check("an unreachable NAS is not rescanned every second", not counted,
          f"{len(counted)} rescans")
    app._local_file_count = real_count

    # ---------------------------------------------------------------- pruning
    app.SETTINGS.update(nas_dir=str(NAS_ROOT), nas_min_free_percent=10)
    app._tl_state["session"] = "tl-20260918-110000"     # pretend this one is running
    free["pct"] = 3.0
    pruned = real_prune(force=True)
    check("prune removes the confirmed frames", pruned == 2, str(pruned))
    check("prune leaves the session being written to alone",
          (s2 / "frame_000001.jpg").exists())
    check("prune tidies away the emptied session folder", not s1.exists())

    free["pct"] = 1.0
    app._tl_state["session"] = ""
    s3 = make_session(local, "tl-20260918-120000", ["frame_000001.jpg"])
    real_prune(force=True)
    check("prune never touches a frame that is not on the NAS",
          (s3 / "frame_000001.jpg").exists())

    # plenty of room, an explicit 0, or the feature off: nothing may be cleared
    free["pct"] = 50.0
    s4 = make_session(local, "tl-20260918-130000", ["frame_000001.jpg"])
    real_flush(force=True)
    check("prune with room is a no-op",
          real_prune(force=True) == 0 and (s4 / "frame_000001.jpg").exists())

    free["pct"] = 1.0
    app.SETTINGS.update(nas_min_free_percent=0)
    check("an explicit 0 never prunes",
          real_prune(force=True) == 0 and (s4 / "frame_000001.jpg").exists())

    app.SETTINGS.update(nas_min_free_percent=10, nas_enabled=False)
    check("switching export off deletes nothing",
          real_prune(force=True) == 0 and (s4 / "frame_000001.jpg").exists())
    app.SETTINGS.update(nas_enabled=True)

    # ---------------------------------------------------------------- finding 6
    app.SETTINGS.update(nas_min_free_percent="10%")
    try:
        app.nas_prune(force=True)
        crashed = False
    except Exception as exc:
        crashed = str(exc)
    check("a non-numeric threshold does not crash the sweep", crashed is False, str(crashed))
    app.SETTINGS.update(nas_min_free_percent=10)

    # ------------------------------------------------- a NAS fault never stops the camera
    shots = []
    saved_shot = app.timelapse_shot

    def boom(*a, **k):
        raise RuntimeError("share went away")

    app.timelapse_shot = lambda: shots.append(1)
    app.nas_flush = boom
    app._tl_state["active"] = True
    app._tl_state["next_shot_at"] = 0.0
    try:
        app.timelapse_tick()
    finally:
        app.nas_flush = lambda *a, **k: None
        app.timelapse_shot = saved_shot
        app._tl_state["active"] = False
    check("a failing NAS does not stop the shot", len(shots) == 1, str(shots))

    # ---------------------------------------------------------------- finding 5
    existing = local / time.strftime("tl-%Y%m%d-%H%M%S")
    existing.mkdir(parents=True, exist_ok=True)
    name = app._new_session_name()
    check("a session name never reuses an existing folder",
          name != existing.name and name.startswith(existing.name), name)

    # ---------------------------------------------------------------- finding 1
    client = app.app.test_client()
    html = client.get("/").data.decode()
    check("the capture form exists", 'id="capture_form"' in html)
    for field in ("nas_enabled", "nas_dir", "nas_min_free_percent"):
        m = re.search(r'<input[^>]*name="%s"[^>]*>' % re.escape(field), html)
        check(f"{field} belongs to the capture form",
              bool(m) and 'form="capture_form"' in m.group(0),
              (m.group(0) if m else "input not found"))

    # ------------------------------------------------- layout and session record
    app.SETTINGS.update(nas_enabled=True, nas_dir=str(NAS_ROOT), nas_min_free_percent=10)
    app._nas_mirrored.clear()
    free["pct"] = 50.0
    s5 = make_session(local, "tl-20261231-235959",
                      ["frame_000001.jpg", "frame_000002.jpg"])
    app._tl_state.update(session="tl-20261231-235959", frames=2, interval_s=300.0,
                         started_at="2026-12-31T23:59:59", ended_at="", save_raw=False)
    app.timelapse_write_meta("tl-20261231-235959")
    check("a session record is written next to the frames", (s5 / "session.json").exists())
    if (s5 / "session.json").exists():
        meta = json.loads((s5 / "session.json").read_text())
        check("the record says how and when it was shot",
              meta.get("interval_s") == 300.0 and meta.get("frames") == 2
              and meta.get("started_at") and meta.get("resolution")
              and "denoise" in meta, str(sorted(meta)))
    app._nas_next_try = 0.0
    real_flush(force=True)
    dated = NAS_ROOT / "2026-12-31" / "tl-20261231-235959"
    check("frames land in a folder dated by day",
          (dated / "frame_000001.jpg").exists() and (dated / "frame_000002.jpg").exists())
    check("sessions are not left loose in the base folder",
          not (NAS_ROOT / "tl-20261231-235959").exists())
    check("the session record reaches the NAS", (dated / "session.json").exists())

    meta_nas = dated / "session.json"
    before = meta_nas.read_text() if meta_nas.exists() else ""
    app._tl_state["frames"] = 9
    app.timelapse_write_meta("tl-20261231-235959")
    app._nas_next_try = 0.0
    real_flush(force=True)
    after = meta_nas.read_text() if meta_nas.exists() else ""

    def _frames(text):
        try:
            return json.loads(text).get("frames")
        except Exception:
            return "?"

    check("a session record that changed is re-sent",
          bool(before) and bool(after) and before != after and '"frames": 9' in after,
          f"NAS frames {_frames(before)} -> {_frames(after)}, differs={before != after}")
    app._nas_next_try = 0.0
    check("an unchanged session record is not re-sent",
          real_flush(force=True)["uploaded"] == 0)

    # ---------------------------------------------------------------- state.json
    (local / "state.json").write_text("{}")
    real_flush(force=True)
    check("state.json is never uploaded", not (NAS_ROOT / "state.json").exists())
    check("state.json is never pruned", (local / "state.json").exists())

finally:
    clean_shm()
    shutil.rmtree(tmp, ignore_errors=True)

width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {extra if not ok else ''}")
    failed += not ok
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
