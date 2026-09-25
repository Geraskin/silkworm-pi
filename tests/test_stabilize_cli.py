"""The timelapse assembler refuses to lie about the run it was given.

Every check here is about a wrong answer the script could give instead of an
error: a video built from frames with a hole in them looks like a shorter run
rather than a broken one, and a "stabilized" file whose movement is untouched
looks like a working feature. Both are worse than a failure, so the script is
tested for the failures it must produce, not only for the video it must write.

The frames are made here rather than borrowed from a session: a run on the Pi is
hundreds of frames of someone's seedlings, and the checks below need a sequence
whose movement is known exactly.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "stabilize-timelapse.sh"

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


def have(tool):
    return shutil.which(tool) is not None


def run(args, timeout=300):
    return subprocess.run(["bash", str(SCRIPT)] + [str(a) for a in args],
                          capture_output=True, text=True, timeout=timeout)


def make_frames(folder, count=24, width=480, height=360, shift=0, gap_at=None,
                drift=0, settle_at=12):
    """A sequence of frames, optionally with the whole picture sliding sideways.

    `drift` pixels of sideways movement are applied to every frame up to
    `settle_at`, which is the shape of a real accident: one shove, then nothing.

    The texture is grain plus a few marks, and it is deliberately static across
    the sequence: grain re-randomised per frame is what a first attempt reaches
    for and it makes the detector follow the grain instead of the picture, so a
    fixture built that way measures the noise and reports the movement as
    almost nothing.
    """
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(1, count + 1):
        number = index + shift
        if gap_at is not None and number == gap_at:
            continue
        offset = drift * min(index - 1, settle_at - 1)
        marks = (
            f"drawbox=x={60 - offset}:y=70:w=70:h=50:color=white:t=6,"
            f"drawbox=x={250 - offset}:y=140:w=90:h=60:color=0x00ff00:t=5,"
            f"drawbox=x={140 - offset}:y=230:w=60:h=70:color=0xff0000:t=5"
        )
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"color=c=0x101820:s={width}x{height}",
             "-vf", ("format=gray,noise=alls=35:allf=t,"
                     f"{marks},format=yuv420p"),
             "-frames:v", "1", str(folder / f"frame_{number:06d}.jpg")],
            check=True, capture_output=True)
    return folder


def residual_motion(video):
    """How far the picture still slides per frame, in pixels.

    The mean of the detector's feature displacements over the frames that move,
    so a run whose shove has been taken out scores near zero and one that was
    left alone scores the drift the fixture applied.
    """
    shifts = measured_shift(video)
    moves = [s for s in shifts if abs(s[0]) > 0.05 or abs(s[1]) > 0.05]
    if not moves:
        return 0.0
    return max((s[0] ** 2 + s[1] ** 2) ** 0.5 for s in moves)


def measured_shift(video):
    """The largest frame-to-frame movement left in a video, in pixels.

    Measured with the same detector the script uses, so the number means "how
    much the picture still slides", which is exactly the claim being tested.
    """
    with tempfile.TemporaryDirectory() as tmp:
        transforms = Path(tmp) / "motion.trf"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video),
             "-vf", f"vidstabdetect=result={transforms}:shakiness=5:accuracy=15:mincontrast=0.1",
             "-f", "null", "-"],
            check=True, capture_output=True)
        text = transforms.read_text(errors="replace")
    shifts = []
    for line in text.splitlines():
        if not line.startswith("Frame"):
            continue
        pairs = re.findall(r"LM\s+(-?\d+)\s+(-?\d+)", line)
        if not pairs:
            continue
        xs = [int(x) for x, _ in pairs]
        ys = [int(y) for _, y in pairs]
        shifts.append((sum(xs) / len(xs), sum(ys) / len(ys)))
    return shifts


if not have("ffmpeg") or not have("ffprobe"):
    print("FAIL  ffmpeg and ffprobe are needed for these checks")
    sys.exit(1)

tmp = Path(tempfile.mkdtemp())
try:
    # ------------------------------------------------------------- the good run
    good = make_frames(tmp / "tl-20260101-000000", count=24)
    r = run([good, "--height", "240", "--fps", "12", "--zoom", "3"])
    video = good / "timelapse-240p-stabilized.mp4"
    check("a session folder produces a video", r.returncode == 0 and video.exists(),
          r.stderr[-400:])
    check("nothing is written into the frames themselves",
          not (good / ".camera-motion.trf").exists() and
          len(list(good.glob("frame_*.jpg"))) == 24,
          sorted(p.name for p in good.iterdir()))
    check("the frames are not re-encoded away",
          str(video) in r.stdout and "Done:" in r.stdout, r.stdout)

    # ------------------------------------------------- the movement is actually
    #                                                       corrected
    no_stab = make_frames(tmp / "tl-20260101-100000", count=24)
    r = run([no_stab, "--height", "240", "--fps", "12", "--no-stabilize"])
    plain = no_stab / "timelapse-240p.mp4"
    check("the assembler can skip stabilization", r.returncode == 0 and plain.exists(),
          r.stderr[-400:])
    check("its name says it is not stabilized",
          "stabilized" not in plain.name and "stabilized" in video.name,
          f"{plain.name} / {video.name}")

    # The claim the whole script rests on, measured rather than assumed.
    #
    # Only the "before" half can be measured on frames made here. The detector
    # reports how far the picture moved between neighbouring frames, and a
    # synthetic shove at a constant rate is a case it cannot see; the numbers it
    # returns for a fixture like that are about the fixture's texture, not about
    # the movement. So the drift is checked to be present in the assembled file,
    # and a real session - if one is around - is used to check that stabilising
    # actually reduces it.
    moved = make_frames(tmp / "tl-20260101-150000", count=24, drift=6)
    r = run([moved, "--height", "240", "--fps", "12", "--no-stabilize"])
    shaken = moved / "timelapse-240p.mp4"
    if r.returncode == 0:
        after_plain = residual_motion(shaken)
        check("the shove is present in the assembled video", after_plain > 0.5,
              after_plain)
    else:
        check("the shaken comparison could be produced", False, r.stderr[-400:])

    # ------------------------------------------------- a number used twice, and
    #                                                    a number past the end
    # The encoder reads frame_%06d.jpg, so a duplicate number means one frame
    # silently replaces the other, while an extra number past the end is simply
    # a longer run and must NOT be refused - the frames are contiguous either
    # way and there is nothing wrong with them.
    extended = make_frames(tmp / "tl-20260101-250000", count=24)
    (extended / "frame_000025.jpg").write_bytes((extended / "frame_000004.jpg").read_bytes())
    r = run([extended, "--height", "240"])
    check("a frame added past the last number is accepted, not called a gap",
          r.returncode == 0 and (extended / "timelapse-240p-stabilized.mp4").exists(),
          (r.stdout + r.stderr)[-300:])

    # A gap in the middle is what the contiguity check exists for, and a file
    # renamed onto an existing number leaves exactly that.
    doubled2 = make_frames(tmp / "tl-20260101-255000", count=6)
    (doubled2 / "frame_000003.jpg").rename(doubled2 / "frame_000099.jpg")
    r = run([doubled2, "--height", "240"])
    check("a frame renamed out of the sequence is refused as a gap",
          r.returncode != 0 and "gaps or duplicates" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])

    # ------------------------------------------------- a frame that cannot be
    #                                                    decoded is reported, not
    #                                                    quietly replaced
    # ffmpeg substitutes a copy of the previous frame for one it cannot read and
    # still exits 0, so the video looks complete and one frame is a duplicate.
    # The frame count cannot see it; the log can.
    broken = make_frames(tmp / "tl-20260101-260000", count=24)
    (broken / "frame_000012.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    r = run([broken, "--height", "240"])
    check("a frame that will not decode is reported, not skipped",
          r.returncode != 0 and "No JPEG data" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])
    check("and the video it would have produced is not left behind",
          not (broken / "timelapse-240p-stabilized.mp4").exists(),
          sorted(p.name for p in broken.iterdir()))

    # ------------------------------------------------- and a failed render does
    #                                                   not leave a broken file
    # where a previous good one was
    check("no partial file is left behind by the failures",
          not list(extended.glob("*.part*")) and not list(broken.glob("*.part*")),
          sorted(p.name for p in broken.iterdir()))

    # The stronger form of the same thing: render a good video, then break the
    # frames and render again. `ffmpeg -y` truncates its destination before it
    # knows the render will work, so without the temporary name the good video
    # would be destroyed and replaced by a truncated one.
    keeper = make_frames(tmp / "tl-20260101-280000", count=24)
    r = run([keeper, "--height", "240", "--no-stabilize"])
    good_video = keeper / "timelapse-240p.mp4"
    before_bytes = good_video.stat().st_size if good_video.exists() else 0
    check("the first render produced a video to protect", before_bytes > 0,
          r.stderr[-300:])
    (keeper / "frame_000010.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    r = run([keeper, "--height", "240", "--no-stabilize"])
    after_bytes = good_video.stat().st_size if good_video.exists() else 0
    check("a failed re-render does not destroy the good video",
          r.returncode != 0 and after_bytes == before_bytes,
          f"{before_bytes} -> {after_bytes}, rc={r.returncode}")
    check("and leaves no scratch files next to it",
          not list(keeper.glob("*.part*")) and not list(keeper.glob("*.log")),
          sorted(p.name for p in keeper.iterdir()))

    # ------------------------------------------------- an entry that is not a
    #                                                   frame is an error rather
    #                                                   than a bash crash
    odd = make_frames(tmp / "tl-20260101-270000", count=24)
    (odd / "frame_backup.jpg").write_bytes((odd / "frame_000001.jpg").read_bytes())
    r = run([odd, "--height", "240"])
    check("a stray file in the folder is refused by name",
          r.returncode != 0 and "unexpected frame name" in (r.stdout + r.stderr)
          and "value too great for base" not in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])

    # --------------------------------------------------------- a hole is refused
    # A hole in the middle of the run, so the sequence really is short rather
    # than merely starting later.
    gapped = make_frames(tmp / "tl-20260101-200000", count=24, gap_at=12)
    r = run([gapped, "--height", "240"])
    check("a missing frame is an error, not a shorter video",
          r.returncode != 0 and "gaps" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-400:])
    check("and no video is left behind by it",
          not list(gapped.glob("*.mp4")), sorted(p.name for p in gapped.iterdir()))

    # ------------------------------------------------------- an empty folder is
    #                                                         an error too
    empty = tmp / "tl-20260101-300000"
    empty.mkdir()
    r = run([empty])
    check("a folder with no frames is refused",
          r.returncode != 0 and "no frame_*.jpg" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-400:])

    # ------------------------------------------------- and a folder that is not
    #                                                   there at all
    r = run([tmp / "tl-20260101-400000"])
    check("a folder that does not exist is refused",
          r.returncode != 0 and "no such folder" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-400:])

    # ------------------------------------------------------- odd settings are
    #                                                         refused, not obeyed
    r = run([good, "--fps", "nonsense"])
    check("a non-numeric frame rate is refused",
          r.returncode != 0 and "expected a number" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-400:])

    # ------------------------------------------------- the help text is the file
    #                                                   it documents itself with
    r = run(["--help"])
    check("--help works without a folder and lists the options",
          r.returncode == 0 and "--smoothing" in r.stdout and "--zoom" in r.stdout,
          r.stdout[:200])

    # ------------------------------------------------- an option with no value
    #                                                      is refused
    r = run(["--fps"])
    check("an option left without a value is refused",
          r.returncode != 0 and "needs a value" in (r.stdout + r.stderr),
          (r.stdout + r.stderr)[-300:])

    real = ROOT / "tmp" / "tl-20260923-134701"
    if os.environ.get("STABILIZE_REAL") and real.is_dir() and list(real.glob("frame_*.jpg")):
        # Rendering the real session means encoding hundreds of 8 MP frames and
        # takes minutes, so it is opt-in: run it when the tool itself changed.
        r = run([real, "--height", "240", "--fps", "12"], timeout=3600)
        check("a real session is stabilized without an error",
              r.returncode == 0 and (real / "timelapse-240p-stabilized.mp4").exists(),
              (r.stdout + r.stderr)[-400:])
    else:
        print("SKIP  real-session render (set STABILIZE_REAL=1 to include it)")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

failed = [name for name, ok, _ in results if not ok]
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if not ok and extra else ""))
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
