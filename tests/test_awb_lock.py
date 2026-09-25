"""A locked run must actually lock the white balance, not just the numbers.

The bug this catches is subtle and was found on real frames: a run locked its
colour gains, but the AWB *mode* stayed on auto, and the ISP kept re-deciding the
colour. The frames therefore had the locked numbers applied and still changed
hue between neighbours - three visible jumps in one run, with the scene darker or
redder after each.

The check is on the controls that are handed to the camera, because that is where
the two settings have to agree. A test that only looked at the lock dictionary
would have passed throughout.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import camera as cam

results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond), extra))


class FakeAwbModeEnum:
    Auto = "AUTO"
    Manual = "MANUAL"
    Incandescent = "INCANDESCENT"


class FakeMeteringEnum:
    CentreWeighted = "CENTRE"


class FakeExposureEnum:
    Normal = "NORMAL"


class FakeLibcamera:
    """Just the enums apply_controls() reaches for, with the shape libcamera has.

    Manual exists here because the whole fix depends on it: the real
    libcamera exposes it, and a build that does not is covered separately below.
    """

    AwbModeEnum = FakeAwbModeEnum
    AeMeteringModeEnum = FakeMeteringEnum
    AeExposureModeEnum = FakeExposureEnum


class FakePicam:
    """Records the controls it is given, without needing a camera."""

    def __init__(self):
        self.got = []
        self.camera_controls = {"AeEnable": object(), "ExposureTime": object(),
                                "AnalogueGain": object(), "AwbEnable": object(),
                                "AwbMode": object(), "ColourGains": object(),
                                "Brightness": object(), "Contrast": object(),
                                "Saturation": object(), "Sharpness": object(),
                                "AeMeteringMode": object(), "AeExposureMode": object(),
                                "NoiseReductionMode": object()}

    def set_controls(self, controls):
        self.got.append(dict(controls))


def controls_for(settings, with_libcamera=True):
    """The controls apply_controls() would push for these settings."""
    c = cam.Camera(Path(tempfile.mkdtemp()))
    c._running = True
    c._picam = FakePicam()
    cam.AVAILABLE = True
    saved_lc = cam.lc
    cam.lc = FakeLibcamera if with_libcamera else None
    try:
        err = c.apply_controls(settings)
    finally:
        cam.lc = saved_lc
    return c._picam.got[-1] if c._picam.got else {}, err


LOCK = {"exposure_us": 6748, "gain": 1.0, "colour_gains": [1.174, 2.232]}

try:
    # ---------------------------------------------------------- a locked run
    got, err = controls_for({"locked": dict(LOCK)})
    check("the exposure is fixed", got.get("AeEnable") is False, got)
    check("the shutter is the measured one", got.get("ExposureTime") == 6748, got)
    check("the gain is the measured one", got.get("AnalogueGain") == 1.0, got)

    check("the colour gains are applied", got.get("ColourGains") == (1.174, 2.232),
          got)

    # The one that matters: with the gains pinned, the auto white balance has to
    # be off. Leaving AwbEnable on (or the mode on Auto) lets the ISP re-decide
    # the colour between frames, which is what made the run change hue.
    check("the white balance is switched off", got.get("AwbEnable") is False, got)

    # AwbMode is the trap. Setting AwbEnable=False on its own was not enough on
    # this hardware: the mode stayed Auto and the ISP kept working the scene out.
    mode = got.get("AwbMode")
    check("the auto white balance mode is not left on Auto",
          mode is not None and "auto" not in str(mode).lower(), mode)
    check("the mode is manual, so the pinned gains hold",
          str(mode).lower() == "manual", mode)

    # ---------------------------------------- a build without libcamera enums
    # The mode cannot be set at all there, and pretending otherwise would leave
    # a control half-applied, so it is left out entirely.
    got, err = controls_for({"locked": dict(LOCK)}, with_libcamera=False)
    check("without libcamera the gains are still pinned",
          got.get("ColourGains") == (1.174, 2.232) and
          got.get("AwbEnable") is False, got)
    check("and no auto mode is sent for a locked run",
          "AwbMode" not in got, got)

    # ------------------------------------------------- a run without a lock
    got, err = controls_for({})
    check("without a lock the auto exposure is left alone", got.get("AeEnable") is True,
          got)
    check("and the white balance stays automatic", got.get("AwbEnable") is True, got)
    check("and no colour gains are forced", "ColourGains" not in got, got)

    # ---------------------------------- a partial lock must not half-apply
    got, err = controls_for({"locked": {"exposure_us": 5000, "gain": 2.0}})
    check("a lock with no colour gains still fixes exposure",
          got.get("AeEnable") is False and got.get("ExposureTime") == 5000, got)
    check("and does not invent colour gains", "ColourGains" not in got, got)
    check("and leaves the white balance automatic in that case",
          got.get("AwbEnable") is True, got)

    # ------------------------------------------- the preset is honoured when
    #                                              there is no lock to override it
    got, err = controls_for({"awb": "incandescent"})
    check("a chosen white-balance preset is used when nothing is locked",
          str(got.get("AwbMode", "")).lower() == "incandescent", got.get("AwbMode"))

finally:
    cam.AVAILABLE = False

failed = [n for n, ok, _ in results if not ok]
for name, ok, extra in results:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  {extra}" if not ok and extra else ""))
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
