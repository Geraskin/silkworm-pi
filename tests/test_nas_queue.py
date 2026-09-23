"""The queue count must not read 0 while a backlog is still waiting to go out."""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app_v2 as app

# The module starts its worker thread on import; keep it out of the way.
real_flush = app.nas_flush
app.nas_flush = lambda *a, **k: None
app.nas_prune = lambda *a, **k: None

tmp = Path(tempfile.mkdtemp())
local, base, share = tmp / "local", tmp / "base", Path("/dev/shm/queue_probe")
for d in (local, base):
    d.mkdir()
shutil.rmtree(share, ignore_errors=True)
share.mkdir(parents=True)

app.TIMELAPSE_DIR = local
app.BASE_DIR = base
session = local / "tl-20260923-120000"
session.mkdir()
for i in range(3):
    (session / f"frame_{i + 1:06d}.jpg").write_bytes(b"x" * 100)

app.SETTINGS.update(nas_enabled=True, nas_dir=str(share), nas_min_free_percent=10)

fails = []


def check(name, cond, extra=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}  {extra if not cond else ''}")
    fails.append(not cond)


# Before any sweep: nothing is known to be on the NAS, so all 3 frames are queued.
app._nas_pending = None
app._nas_next_try = 0.0
before = app.nas_pending()
check("a backlog is reported before the first sweep", before == 3, before)
check("the status route agrees",
      app.timelapse_status()["nas_pending"] == 3,
      app.timelapse_status()["nas_pending"])
check("asking twice does not walk the cache again",
      app.nas_pending() == before, app.nas_pending())

# After a sweep that uploads everything the queue is genuinely empty.
app._nas_pending = None
r = real_flush(force=True)
check("the sweep uploads the backlog", r["ok"] and r["uploaded"] == 3, r)
check("the queue is empty once it is up", app.nas_pending() == 0, app.nas_pending())

# A share that goes away must not read as an empty queue either.
app._nas_pending = None
app._nas_next_try = 0.0
shutil.rmtree(share)
r = real_flush(force=True)
check("a missing share reports the local backlog",
      r["pending"] == 3 and app.nas_pending() == 3, (r, app.nas_pending()))

shutil.rmtree(tmp, ignore_errors=True)
shutil.rmtree("/dev/shm/queue_probe", ignore_errors=True)
print(f"\n{sum(not f for f in fails)}/{len(fails)} passed")
raise SystemExit(1 if any(fails) else 0)
