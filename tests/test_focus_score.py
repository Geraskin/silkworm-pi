"""What the focus sharpness meter has to get right, on any machine.

No camera, no Pi, no privileges: the metric is a pure function of an image, so it
is fed synthetic ones with a known amount of detail. The point of the suite is the
shape of the answer - it must fall as a frame goes soft, stay put when the light
changes, and not be fooled by sensor noise, because focus mode deliberately keeps
the denoiser off.

The app-level part checks that the peak the UI shows is per focusing attempt and
that reading the endpoint never touches the camera.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camera as cam
from PIL import Image, ImageFilter

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


def textured(size=(320, 240), background=60, ink=200, period=8):
    """A scene with fine detail, like a subject you would focus on."""
    img = Image.new("L", size, background)
    px = img.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            if ((x % period < period // 2) ^ (y % period < period // 2)
                    and w // 6 < x < w - w // 6 and h // 6 < y < h - h // 6):
                px[x, y] = ink
    return img.convert("RGB")


def noisy(size=(320, 240), level=8, base=128, seed=1):
    """Flat field plus sensor noise - no detail at all."""
    import random
    rng = random.Random(seed)
    img = Image.new("L", size, base)
    px = img.load()
    w, h = size
    for y in range(h):
        for x in range(w):
            px[x, y] = max(0, min(255, base + rng.randint(-level, level)))
    return img.convert("RGB")


def scaled(img, factor):
    return img.point(lambda v: min(255, int(v * factor)))


# ---------------------------------------------------------------- the metric
sharp = textured()
blurs = (0, 0.5, 1, 2, 4, 8)
scores = {r: cam.sharpness_score(sharp if r == 0
                                 else sharp.filter(ImageFilter.GaussianBlur(r)))
          for r in blurs}
# Above the noise floor the direction is what matters: every step towards focus
# has to raise the number. Below it the frame is mush and the last digit is
# 8-bit rounding, so those steps are only required not to climb.
above_floor = [r for r in blurs if scores[r] > 5.0]
check("coming into focus raises the score, step by step",
      all(scores[a] > scores[b] for a, b in zip(above_floor, above_floor[1:])),
      " > ".join(f"{r}:{scores[r]}" for r in above_floor))
check("a hopelessly soft frame reads as nothing",
      max(scores[r] for r in (4, 8)) < 0.1 * scores[0],
      f"blur 4/8: {scores[4]}/{scores[8]}, sharp: {scores[0]}")
check("real detail scores far above a flat frame",
      cam.sharpness_score(sharp) > 20 and
      cam.sharpness_score(Image.new("RGB", (320, 240), (128, 128, 128))) == 0.0,
      f"detail {cam.sharpness_score(sharp)}, flat "
      f"{cam.sharpness_score(Image.new('RGB', (320, 240), (128, 128, 128)))}")

# Focus mode leaves the ISP denoiser off, so a frame full of sensor noise must not
# read the way a soft subject does. The measure has a noise floor - nothing in a
# dark frame is in focus - but the floor has to stay far below real detail.
detail = cam.sharpness_score(sharp)
noise = {lvl: cam.sharpness_score(noisy(level=lvl)) for lvl in (8, 16, 32)}
slightly_soft = cam.sharpness_score(sharp.filter(ImageFilter.GaussianBlur(1)))
check("noise does not outscore a slightly soft subject",
      noise[8] < 0.5 * slightly_soft,
      f"noise +-8 {noise[8]}, subject {slightly_soft}")
check("the noise floor stays far below a focused frame",
      noise[16] < 0.2 * detail, f"noise +-16 {noise[16]}, detail {detail}")
check("a heavily defocused frame cannot pass for a sharp one",
      cam.sharpness_score(sharp.filter(ImageFilter.GaussianBlur(8))) < 0.1 * detail,
      f"{cam.sharpness_score(sharp.filter(ImageFilter.GaussianBlur(8)))} vs {detail}")
check("more noise, more floor - the guard is not hiding a broken metric",
      noise[8] < noise[16] < noise[32],
      f"{noise[8]} < {noise[16]} < {noise[32]}")

# 2-3 pixels is the finest detail a 1:1 crop can show, and exactly what focusing
# by hand is for. The blur before measuring is a Gaussian for this reason: a box
# of the same width nulls out texture at its own period and the meter would dip
# on a scene that is perfectly sharp.
def stripes(period, size=(320, 240)):
    img = Image.new("L", size, 60)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = 200 if x % period < period // 2 else 60
    return img.convert("RGB")

fine, coarse = cam.sharpness_score(stripes(3)), cam.sharpness_score(stripes(8))
check("detail at the sensor's own scale is not blurred away",
      fine > 0.25 * coarse, f"3 px detail {fine}, 8 px detail {coarse}")

# The number has to follow the focus, not the exposure: the lamp dims, the sun
# goes behind a cloud, and the meter should not read that as a focus change.
dark = cam.sharpness_score(scaled(sharp, 0.6))
lit = cam.sharpness_score(sharp)
check("a dimmer frame scores about the same", abs(dark - lit) <= 0.2 * lit,
      f"{lit} -> {dark}")

# ... but dividing by the brightness has to stop somewhere. This frame is what a
# covered lens looks like on the real camera - mean brightness 1.4 out of 255,
# nothing above 9 - and it scored 218, the best number the meter had ever shown,
# before the divisor was given a floor. Without the floor this generator scores
# about 225 too, so the check is the one that was failing on the Pi.
def covered_lens(size=(320, 240), seed=5):
    import random
    rng = random.Random(seed)
    img = Image.new("L", size, 0)
    px = img.load()
    for y in range(size[1]):
        for x in range(size[0]):
            px[x, y] = rng.choices((0, 1, 2, 4, 9), weights=(88, 6, 3, 2, 1))[0]
    return img.convert("RGB")

black = cam.sharpness_score(covered_lens())
check("a covered lens reads as nothing to focus on", black < 20,
      f"covered lens {black}, sharp subject {cam.sharpness_score(sharp)}")

# Vertical and horizontal detail are equally sharp; the meter must not have a
# preferred direction.
vert = stripes(8)
horiz = vert.transpose(Image.Transpose.ROTATE_90)
v, h = cam.sharpness_score(vert), cam.sharpness_score(horiz)
check("direction of the detail does not matter", abs(v - h) <= 0.02 * max(v, h),
      f"vertical {v}, horizontal {h}")

check("an image too small to measure is refused, not guessed",
      cam.sharpness_score(Image.new("RGB", (2, 2), (0, 0, 0))) == 0.0)
check("no image means no score", cam.sharpness_score(None) == 0.0)

# A wide focus crop must not cost more to measure than the default one: on a Pi 3
# a 1280x960 metric run takes 186 ms, longer than capturing the frame itself.
whole = Image.new("RGB", (320, 240), (10, 10, 10))
check("a crop that fits the window is measured whole",
      cam.score_window(whole) is whole)
wide = cam.score_window(Image.new("RGB", (1280, 960), (10, 10, 10)))
check("a wide crop is measured in the middle, at the default size",
      wide.size == cam.FOCUS_SCORE_WINDOW, f"{wide.size}")
board = Image.new("RGB", (1280, 960), (255, 255, 255))
board.paste((0, 0, 0), (320, 240, 960, 720))     # white frame, black centre only
check("the measured window is the centre of the crop",
      cam.score_window(board).getextrema() == ((0, 0), (0, 0), (0, 0)),
      f"a centre of pure black came back as {cam.score_window(board).getextrema()}")

# Off the Pi the capture path has to fail cleanly: the focus stream turns a
# (None, None) into a retry, so a tuple is the contract.
frame = cam.Camera(Path("/tmp"), preview_size=(64, 48), fps=5)
data, score = frame.focus_frame((32, 24))
check("focus_frame reports (None, None) when there is no camera",
      data is None and score is None, f"got {data!r}, {score!r}")


class _BadFrame:
    """A frame as a mid-restart capture can return it: no height, no width."""

    shape = (3072,)
    ndim = 1


class _StubCamera(cam.Camera):
    """A Camera that hands back whatever the test wants, as if from the sensor."""

    def __init__(self, array):
        super().__init__(Path("/tmp"), preview_size=(64, 48), fps=5)
        self._array = array
        self._running = True
        self.lock = None

    def _main_array(self):
        return self._array

    def _wait_for_encoder(self, grace=1.2):
        return None


was_available = cam.AVAILABLE
cam.AVAILABLE = True          # the capture path only runs on a Pi
try:
    for label, bad in (("a one-dimensional frame", _BadFrame()),
                       ("no frame at all", None)):
        stub = _StubCamera(bad)
        try:
            data, score = stub.focus_frame((32, 24))
            raised = None
        except Exception as exc:
            data = score = None
            raised = exc
        check(f"{label} is a retry, not a crash",
              data is None and score is None and raised is None,
              f"raised {raised!r}")
    one_bad_frame = _StubCamera(_BadFrame())
    one_bad_frame.focus_frame((32, 24))
    check("a bad frame leaves a reason in last_error",
          bool(one_bad_frame.last_error), f"last_error {one_bad_frame.last_error!r}")
finally:
    cam.AVAILABLE = was_available

# ------------------------------------------------- the meter the UI talks to
# Importing the app starts its background worker (as in the other test files), so
# keep the NAS out of it.
import app_v2 as app

app.nas_flush = lambda *a, **k: None
app.nas_prune = lambda *a, **k: None
client = app.app.test_client()

empty = client.get("/focus/score").get_json()
check("an untouched meter reads as 'nothing measured yet'",
      empty["ok"] is False and empty["score"] is None and empty["peak"] is None
      and empty["history"] == [])

app.focus_note_score(12.0, (640, 480))
app.focus_note_score(48.5, (640, 480))
app.focus_note_score(30.0, (640, 480))
state = client.get("/focus/score").get_json()
check("the meter reports the last score and the best one",
      state["score"] == 30.0 and state["peak"] == 48.5,
      f"score {state['score']}, peak {state['peak']}")
check("the best one is shown as a percentage of itself",
      state["percent"] == round(100.0 * 30.0 / 48.5, 1), f"{state['percent']}")
check("the history keeps the order of the samples",
      state["history"] == [12.0, 48.5, 30.0], f"{state['history']}")
check("the crop the score was measured on comes along", state["crop"] == "640x480")

reset = client.post("/focus/score").get_json()
check("Reset clears the peak, not just the current value",
      reset["peak"] is None and reset["frames"] == 0 and reset["history"] == [])
app.focus_note_score(40.0, (640, 480))
app.focus_note_score(9.0, (640, 480))
after = client.get("/focus/score").get_json()
check("after a reset the peak starts from the new frames",
      after["peak"] == 40.0 and after["score"] == 9.0,
      f"peak {after['peak']}, score {after['score']}")
check("no frame measured means no age to report",
      client.post("/focus/score").get_json()["age_ms"] is None)

# Reading the meter must never capture: on a machine without a camera the page
# would still have to answer, and on the Pi a capture here would fight the stream.
check("reading the meter does not need a camera",
      client.get("/focus/score").status_code == 200 and not cam.AVAILABLE,
      f"picamera2 available: {cam.AVAILABLE}")

width = max(len(n) for n, _, _ in results)
failed = 0
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {extra if not ok else ''}")
    failed += not ok
print(f"\n{len(results) - failed}/{len(results)} passed")
raise SystemExit(1 if failed else 0)
