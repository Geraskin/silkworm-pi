"""Camera backend for Silkworm Pi (picamera2 / libcamera).

A single Picamera2 instance serves three things:
  * live MJPEG preview  -> the low-res "lores" stream, hardware-encoded;
  * still captures      -> the "main" stream at the selected resolution
                           (JPEG, optionally plus a DNG/RAW file);
  * H.264 video         -> recorded to a file.

Using one camera process (instead of spawning rpicam-still) means the preview
keeps running while a photo is taken.

This module is only functional on a Raspberry Pi with picamera2 installed.
Everywhere else it imports cleanly and reports AVAILABLE == False.
"""
from __future__ import annotations

import io
import os
import threading
import time
import traceback
from pathlib import Path

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder, JpegEncoder, MJPEGEncoder
    from picamera2.outputs import FileOutput

    AVAILABLE = True
    IMPORT_ERROR = ""
except Exception as exc:  # not a Pi, or picamera2 not installed
    Picamera2 = None
    H264Encoder = JpegEncoder = MJPEGEncoder = FileOutput = None
    AVAILABLE = False
    IMPORT_ERROR = str(exc)

try:
    from libcamera import Transform
    from libcamera import controls as lc
except Exception:  # pragma: no cover
    Transform = None
    lc = None

try:
    from PIL import Image

    HAVE_PIL = True
except Exception:
    HAVE_PIL = False


AWB_MODES = ["auto", "incandescent", "tungsten",
             "fluorescent", "indoor", "daylight", "cloudy"]
METERING_MODES = ["centre", "spot", "average"]
EXPOSURE_MODES = ["normal", "sport", "long"]
DENOISE_MODES = ["off", "fast", "high_quality", "minimal"]

# An operation stuck longer than this is not coming back; see Camera._watchdog.
WATCHDOG_S = float(os.environ.get("CAMWEB_WATCHDOG_S", "45") or 45)

_AWB_ENUM = {
    "auto": "Auto",
    "incandescent": "Incandescent",
    "tungsten": "Tungsten",
    "fluorescent": "Fluorescent",
    "indoor": "Indoor",
    "daylight": "Daylight",
    "cloudy": "Cloudy",
}
_METERING_ENUM = {"centre": "CentreWeighted",
                  "spot": "Spot", "average": "Matrix"}
_EXPOSURE_ENUM = {"normal": "Normal", "sport": "Short", "long": "Long"}
# libcamera 0.7.x exposes no NoiseReductionMode enum, so use the raw values:
# 0=Off, 1=Fast, 2=HighQuality, 3=Minimal.
_DENOISE_VALUE = {"off": 0, "fast": 1, "high_quality": 2, "minimal": 3}

# Shutter speeds offered in the UI: label -> microseconds.
SHUTTER_SPEEDS = [
    ("1/1000 s", 1000),
    ("1/500 s", 2000),
    ("1/250 s", 4000),
    ("1/125 s", 8000),
    ("1/100 s", 10000),
    ("1/60 s", 16667),
    ("1/50 s", 20000),
    ("1/30 s", 33333),
    ("1/15 s", 66667),
    ("1/10 s", 100000),
]

# The Pi's hardware H.264 encoder cannot encode the full 8 MP mode; recording is
# capped to one of these sizes and the still resolution is restored afterwards.
VIDEO_SIZES = [(1920, 1080), (1640, 1232), (640, 480)]

# Preview JPEG quality. The hardware MJPEG encoder exposes no quality knob, so
# the preview uses the software JpegEncoder with 4:4:4 chroma, which removes the
# colour artifacts that are visible at this small size.
PREVIEW_JPEG_QUALITY = 92


class _Stream(io.BufferedIOBase):
    """File-like sink that keeps the most recent MJPEG frame."""

    def __init__(self) -> None:
        self.frame: bytes | None = None
        self.seq = 0
        self.condition = threading.Condition()

    def write(self, buf) -> int:  # noqa: D401 - io protocol
        with self.condition:
            self.frame = bytes(buf)
            self.seq += 1
            self.condition.notify_all()
        return len(buf)

    def latest(self, timeout: float = 2.0) -> bytes | None:
        with self.condition:
            if self.frame is None:
                self.condition.wait(timeout)
            return self.frame

    def next_frame(self, last_seq, timeout: float = 2.0):
        """Wait for a frame newer than `last_seq`; returns (frame, seq)."""
        with self.condition:
            if self.frame is None or self.seq == last_seq:
                self.condition.wait(timeout)
            return self.frame, self.seq


class Camera:
    """Owns the single Picamera2 instance and serialises access to it."""

    def __init__(self, base_dir, preview_size=(640, 480), fps=15):
        self.base_dir = Path(base_dir)
        self.preview_size = tuple(preview_size)
        self.fps = int(fps)
        self.preview_bitrate = 15.0        # Mbps for the hardware MJPEG encoder
        self.preview_enabled = True
        # Set by the focus helper: capture_array("main") and the preview encoder
        # cannot use the camera at the same time, so the preview stands down.
        # A deadline rather than a flag, so a focus stream that dies without
        # cleaning up cannot wedge the preview for ever.
        self.preview_blocked_until = 0.0
        self.last_error = ""
        # When the encoder was last stopped: capture_array() called straight
        # after stop_recording() can block for ever, so captures wait it out.
        self._encoder_stopped_at = 0.0
        # Watchdog state. A call wedged inside libcamera never returns and never
        # raises, so without this the app looks healthy while quietly producing
        # no frames until somebody restarts the service by hand.
        self._wd_lock = threading.Lock()
        self._wd_label = ""
        self._wd_since = 0.0
        threading.Thread(target=self._watchdog, daemon=True).start()
        self._lock = threading.RLock()
        self._stream = _Stream()
        self._picam = None
        self._running = False
        self._streaming = False
        self._recording = False
        self._still_size = (1640, 1232)
        self._hflip = False
        self._vflip = False
        self._video_restore = None
        self._last_settings = {}

    # ------------------------------------------------------------------ setup
    def start(self, still_size=(1640, 1232), hflip=False, vflip=False,
              preview_size=None) -> bool:
        """Configure and start the camera. Safe to call more than once."""
        if not AVAILABLE:
            return False
        with self._lock:
            if self._running:
                return True
            if self._picam is not None:
                try:
                    self._picam.close()
                except Exception:
                    pass
                self._picam = None
            self._still_size = tuple(still_size)
            self._hflip = bool(hflip)
            self._vflip = bool(vflip)
            if preview_size is not None:
                self.preview_size = tuple(preview_size)
            self._picam = Picamera2()
            options = {
                "main": {"size": self._still_size, "format": "RGB888"},
                "lores": {"size": self.preview_size, "format": "YUV420"},
                "controls": {"FrameRate": self.fps},
            }
            if Transform is not None and (self._hflip or self._vflip):
                options["transform"] = Transform(
                    hflip=self._hflip, vflip=self._vflip)
            config = self._picam.create_video_configuration(**options)
            self._picam.configure(config)
            self._picam.start()
            self._running = True
        return True

    def reconfigure(self, still_size, hflip=False, vflip=False,
                    preview_size=None, preview_bitrate=None, force=False) -> bool:
        """Restart the camera if any of its configuration changed.

        The preview bitrate is part of the configuration because the hardware
        MJPEG encoder only picks it up from a fresh configure. `force` restarts
        even when nothing changed, which the focus helper needs: it reads
        full-resolution frames, and doing that while the preview encoder is
        alive can deadlock the camera.
        """
        new_preview = tuple(
            preview_size) if preview_size is not None else self.preview_size
        try:
            rate = float(
                preview_bitrate if preview_bitrate is not None else self.preview_bitrate or 0)
        except Exception:
            rate = 0.0
        want = (tuple(still_size), bool(hflip), bool(vflip), new_preview, rate)
        have = (self._still_size, self._hflip, self._vflip,
                self.preview_size, self.preview_bitrate)
        if want == have and not force:
            return False
        self.stop()
        self.preview_bitrate = rate
        self.start(still_size, hflip, vflip, new_preview)
        return True

    def set_preview_enabled(self, enabled) -> bool:
        """Turn the preview stream on/off without touching the camera config.

        The encoder is deliberately not stopped here: see stop_stream().
        """
        enabled = bool(enabled)
        if enabled == self.preview_enabled:
            return False
        self.preview_enabled = enabled
        return True

    def _teardown(self) -> None:
        """Stop the preview and release the camera device."""
        with self._lock:
            self.stop_stream()
            if self._picam is not None:
                try:
                    if self._running:
                        self._picam.stop()
                except Exception:
                    pass
                try:
                    # Release the device, otherwise a restart fails with
                    # "Device or resource busy".
                    self._picam.close()
                except Exception:
                    pass
            self._picam = None
            self._running = False

    def stop(self) -> None:
        with self._lock:
            self.stop_video(restore=False)
            self._teardown()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def still_size(self):
        return self._still_size

    # ---------------------------------------------------------------- preview
    def _preview_encoder(self):
        """Hardware MJPEG encoder; quality is controlled by the bitrate."""
        try:
            if self.preview_bitrate and float(self.preview_bitrate) > 0:
                return MJPEGEncoder(bitrate=int(float(self.preview_bitrate) * 1_000_000))
            return MJPEGEncoder()
        except Exception:
            # No hardware encoder on this platform: fall back to software JPEG.
            return JpegEncoder(q=PREVIEW_JPEG_QUALITY, colour_subsampling="444")

    def start_stream(self) -> bool:
        if not AVAILABLE or not self._running or self._streaming or self._recording:
            return self._streaming
        with self._lock:
            encoder = self._preview_encoder()
            try:
                # Encode the low-res stream for a light, smooth preview.
                self._picam.start_recording(
                    encoder, FileOutput(self._stream), name="lores")
            except TypeError:
                # Older picamera2 without the `name` argument: stream main.
                self._picam.start_recording(encoder, FileOutput(self._stream))
            self._streaming = True
        return True

    def stop_stream(self) -> None:
        """Stop the preview encoder. ONLY safe as part of a camera teardown.

        picamera2 can block for ever in capture_array() when it follows a
        standalone stop_recording(), and it does so silently - the capture never
        returns and never raises, so the worker just stops making frames. The
        encoder is therefore left running and is stopped in _teardown(), where
        the camera is closed immediately afterwards.
        """
        with self._lock:
            if self._picam and self._streaming:
                try:
                    self._picam.stop_recording()
                except Exception:
                    pass
                self._encoder_stopped_at = time.monotonic()
            self._streaming = False

    def _note_start(self, label) -> None:
        with self._wd_lock:
            self._wd_label = label
            self._wd_since = time.monotonic()

    def _note_end(self) -> None:
        with self._wd_lock:
            self._wd_since = 0.0

    def _watchdog(self) -> None:
        """Exit if a camera operation has been stuck past WATCHDOG_S.

        Waiting longer cannot help - the call is not coming back - so the process
        is ended and systemd (Restart=always) brings the service up again in a
        few seconds. A running timelapse resumes from state.json, so the cost is a
        short gap rather than a camera that is dead until someone notices.
        """
        while True:
            time.sleep(5.0)
            with self._wd_lock:
                label, since = self._wd_label, self._wd_since
            if not since:
                continue
            stuck = time.monotonic() - since
            if stuck < WATCHDOG_S:
                continue
            self.last_error = f"{label} stuck for {stuck:.0f}s"
            print(f"watchdog: {self.last_error}; exiting so the service restarts",
                  flush=True)
            os._exit(1)

    def _main_array(self):
        """One full-resolution frame as an array.

        Deliberately a still capture request rather than capture_array(): the
        latter is tied to the video stream buffers and can block for ever once
        the preview encoder has been stopped, which is what wedged the camera.
        """
        self._note_start("still capture")
        try:
            request = self._picam.capture_request()
            try:
                return request.make_array("main")
            finally:
                request.release()
        finally:
            self._note_end()

    def _wait_for_encoder(self, grace=1.2) -> None:
        """Let the encoder finish letting go of the camera.

        picamera2 can block indefinitely in capture_array() if it is called
        immediately after stop_recording(). Every capture goes through here so
        that neither the photo path nor the focus helper can hit it.
        """
        left = grace - (time.monotonic() - self._encoder_stopped_at)
        if left > 0:
            time.sleep(left)

    def frames(self):
        """Yield multipart MJPEG chunks for an HTTP response.

        Each camera frame is sent exactly once (the encoder replaces the most
        recent frame as fast as the sensor produces them).
        """
        seq = None
        try:
            while True:
                if (not self.preview_enabled
                        or time.monotonic() < self.preview_blocked_until):
                    # Keep the response open and leave the encoder alone. Stopping
                    # it here would poison the next still capture, and the browser
                    # would be left with a blank image it never asks for again.
                    time.sleep(0.2)
                    continue
                # Restart the preview after a reconfigure/recording, but never
                # while video is recording (both encoders share the V4L2 device).
                if not self._recording and not self._streaming:
                    self.start_stream()
                frame, seq = self._stream.next_frame(seq)
                if frame is not None:
                    yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
        finally:
            # The encoder is not stopped here on purpose: a client going away must
            # not leave the camera in the state that deadlocks the next capture.
            pass

    # ----------------------------------------------------------------- stills
    def capture(self, jpeg_path, quality=93, raw_path=None, target_size=None, rotate=0):
        """Capture a still. Returns (ok, error_message)."""
        if not AVAILABLE or not self._running:
            return False, "Camera not available"
        with self._lock:
            try:
                if HAVE_PIL:
                    self._wait_for_encoder()
                    array = self._main_array()
                    # libcamera's "RGB888" is V4L2 RGB24, which is stored as BGR
                    # in memory; swap the channels so PIL writes correct colours.
                    image = Image.fromarray(array[..., ::-1].copy())
                    try:
                        rotate = int(rotate) % 360
                    except Exception:
                        rotate = 0
                    if rotate in (90, 270):
                        # The Pi ISP cannot rotate (only flip), so 90/270 are done
                        # in software; a negative angle is clockwise in PIL.
                        image = image.rotate(-rotate, expand=True)
                    if target_size and image.size != tuple(target_size):
                        image = image.resize(tuple(target_size), Image.LANCZOS)
                    image.save(str(jpeg_path), quality=int(quality))
                    if raw_path is not None:
                        self._save_dng(raw_path)
                else:
                    request = self._picam.capture_request()
                    try:
                        request.save("main", str(jpeg_path))
                        if raw_path is not None:
                            request.save_dng(str(raw_path))
                    finally:
                        request.release()
                return True, ""
            except Exception as exc:
                return False, str(exc)

    def _save_dng(self, raw_path) -> None:
        request = self._picam.capture_request()
        try:
            request.save_dng(str(raw_path))
        finally:
            request.release()

    # ------------------------------------------------------------------ focus
    def focus_jpeg(self, crop=(640, 480), quality=90):
        """Full-resolution frame, 1:1 centre crop, returned as JPEG bytes.

        No scaling: one sensor pixel maps to one pixel of the result, so focus
        can be judged objectively. Returns None on failure.
        """
        if not AVAILABLE or not self._running or not HAVE_PIL:
            return None
        try:
            # Same lock as capture(): the still path and the preview encoder must
            # not touch the camera at the same time, and without this the capture
            # can block for ever instead of failing.
            with self._lock:
                self._wait_for_encoder()
                array = self._main_array()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        self.last_error = ""
        h, w = array.shape[:2]
        cw = max(16, min(int(crop[0]), w))
        ch = max(16, min(int(crop[1]), h))
        x = (w - cw) // 2
        y = (h - ch) // 2
        region = array[y:y + ch, x:x + cw][..., ::-1].copy()  # BGR -> RGB
        buf = io.BytesIO()
        Image.fromarray(region).save(buf, format="JPEG", quality=int(quality))
        return buf.getvalue()

    # ------------------------------------------------------------------ video
    def _video_size(self):
        """Recording resolution: the still size if the encoder supports it."""
        if self._still_size in VIDEO_SIZES:
            return self._still_size
        return (1920, 1080)

    def start_video(self, path, duration=None):
        """Record H.264 to `path`. Returns (ok, error_message)."""
        if not AVAILABLE or not self._running:
            return False, "Camera not available"
        with self._lock:
            if self._recording:
                return False, "Already recording"

            video_size = self._video_size()
            restore = self._still_size if video_size != self._still_size else None
            if restore is not None:
                self.stop()
                self.start(video_size, self._hflip, self._vflip)
                self.apply_controls(self._last_settings)
            self._video_restore = restore

            self._recording = True  # gate preview restarts before stopping it
            was_streaming = self._streaming
            self.stop_stream()
            if was_streaming:
                # Give the V4L2 encoder a moment to release the device.
                time.sleep(0.3)
            try:
                self._picam.start_recording(
                    H264Encoder(), FileOutput(str(path)))
            except Exception as exc:
                traceback.print_exc()
                self._recording = False
                self._video_restore = None
                return False, str(exc)
        if duration:
            threading.Timer(float(duration), self.stop_video).start()
        return True, ""

    def stop_video(self, restore: bool = True) -> bool:
        with self._lock:
            if self._picam and self._recording:
                try:
                    self._picam.stop_recording()
                except Exception:
                    pass
            was = self._recording
            self._recording = False
            target = self._video_restore if restore else None
            self._video_restore = None
            if target is not None:
                self._teardown()
                self.start(target, self._hflip, self._vflip)
                self.apply_controls(self._last_settings)
        return was

    # --------------------------------------------------------------- settings
    def apply_controls(self, s) -> str:
        """Push app settings to libcamera controls. Returns an error string."""
        if not AVAILABLE or not self._running:
            return ""
        self._last_settings = dict(s)
        controls = {}
        try:
            if s.get("manual_exposure"):
                controls["AeEnable"] = False
                controls["ExposureTime"] = int(s.get("shutter", 10000))
                controls["AnalogueGain"] = float(s.get("gain", 1.0))
            else:
                controls["AeEnable"] = True
                controls["ExposureValue"] = float(s.get("ev", 0.0))

            controls["Brightness"] = float(s.get("brightness", 0.0))
            controls["Contrast"] = float(s.get("contrast", 1.0))
            controls["Saturation"] = float(s.get("saturation", 1.0))
            controls["Sharpness"] = float(s.get("sharpness", 1.0))

            if lc is not None:
                controls["AwbEnable"] = True
                awb = _AWB_ENUM.get(s.get("awb", "auto"), "Auto")
                controls["AwbMode"] = getattr(
                    lc.AwbModeEnum, awb, lc.AwbModeEnum.Auto)
                meter = _METERING_ENUM.get(
                    s.get("metering", "centre"), "CentreWeighted")
                controls["AeMeteringMode"] = getattr(
                    lc.AeMeteringModeEnum, meter, lc.AeMeteringModeEnum.CentreWeighted
                )
                expo = _EXPOSURE_ENUM.get(
                    s.get("exposure", "normal"), "Normal")
                controls["AeExposureMode"] = getattr(
                    lc.AeExposureModeEnum, expo, lc.AeExposureModeEnum.Normal
                )
                controls["NoiseReductionMode"] = _DENOISE_VALUE.get(
                    s.get("denoise", "fast"), 1
                )

            # Hflip/Vflip are not libcamera controls on the Pi: they are applied
            # through the configuration transform (see start/ensure_orientation).
            supported = getattr(self._picam, "camera_controls", None)
            if supported:
                controls = {k: v for k, v in controls.items()
                            if k in supported}

            self._picam.set_controls(controls)
            return ""
        except Exception as exc:
            return str(exc)

    def info(self) -> dict:
        return {
            "available": AVAILABLE,
            "import_error": IMPORT_ERROR,
            "running": self._running,
            "streaming": self._streaming,
            "recording": self._recording,
            "still_size": list(self._still_size),
            "preview_size": list(self.preview_size),
            "preview_bitrate": self.preview_bitrate,
            "preview_enabled": self.preview_enabled,
            "pil": HAVE_PIL,
        }


def _selftest() -> None:
    """Quick capability probe: `python3 camera.py`."""
    print("picamera2 available:", AVAILABLE, IMPORT_ERROR)
    print("Pillow:", HAVE_PIL)
    print("Transform:", Transform is not None)
    print("Shutter speeds:", SHUTTER_SPEEDS)


def _smoke_test(size=(1640, 1232)) -> None:
    """Functional check: `python3 camera.py smoke [WxH]`.

    Starts the camera, grabs MJPEG frames, captures JPEG+DNG, records a short
    H.264 clip and reports file sizes. Leaves nothing running.
    """
    _selftest()
    if not AVAILABLE:
        return
    cam = Camera("/tmp")
    if not cam.start(size):
        print("camera start: FAILED")
        return
    print("camera start: OK", cam.info())

    cam.start_stream()
    time.sleep(1.5)
    frame = cam._stream.latest(timeout=3)
    print("mjpeg frame bytes:", len(frame) if frame else None)

    ok, err = cam.capture("/tmp/_smoke.jpg", quality=90,
                          raw_path="/tmp/_smoke.dng")
    print("capture:", ok, err)
    for path in ("/tmp/_smoke.jpg", "/tmp/_smoke.dng"):
        print("  ", path, os.path.getsize(path)
              if os.path.exists(path) else "MISSING")

    ok, err = cam.start_video("/tmp/_smoke.h264")
    print("start_video:", ok, err)
    time.sleep(1.5)
    cam.stop_video()
    size = os.path.getsize(
        "/tmp/_smoke.h264") if os.path.exists("/tmp/_smoke.h264") else "MISSING"
    print("   video bytes:", size)

    cam.stop()
    print("SMOKE DONE")


if __name__ == "__main__":
    import sys

    argv = sys.argv[1:]
    if "smoke" in argv:
        smoke_size = (1640, 1232)
        for arg in argv:
            if "x" in arg and arg.split("x")[0].isdigit():
                w, h = arg.split("x")
                smoke_size = (int(w), int(h))
        _smoke_test(smoke_size)
    else:
        _selftest()
