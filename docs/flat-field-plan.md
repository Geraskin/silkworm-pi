# Removing the illumination gradient from timelapse frames

A plan for the second half of the work: the pink left-to-right gradient that is
visible in every frame of a run and that the camera cannot be talked out of.

## Status

**Not started.** Nothing here is built. It waits on a flat-field frame taken on
the Pi, which has not been captured: an early attempt was taken with the light
off, so it measured the sensor's dark current instead of the lighting.

The colour jumps that appeared in the *same* frames are a separate fault and are
**already fixed** — that was the white-balance mode being left on automatic while
the gains were pinned. See `tests/test_awb_lock.py`.

## What the investigation established

The gradient is **uneven illumination**, not a camera fault. The evidence:

| Hypothesis | Verdict | Measurement |
|---|---|---|
| Vignetting | No | The profile is linear, not radial |
| Dark current | No | Spread scales 6/10/17/26 at gain 1/2/4/8 over 1 s, but scaled to the run's 6748 µs it is **0.04 levels** against the **49** actually seen |
| Cable / readout | No | The vertical profile is flat, and two different exposures give the *same* shape |
| BGR swap in code | No | `rotation` was 0 and the conversion is verified correct |
| Lens shading in the app | No | No `LensShading`, `BlackLevel` or `ScalerCrop` control is set anywhere |

Decisive numbers: a 20 ms frame under even light has a spread of **1** level; a
run frame has a spread of **49**. And the shape is stable for the whole two-day
run — frame 1 and frame 286 match.

Because it is stable, it can be measured once and divided out.

## The core idea

A **flat field**: photograph something evenly lit and colour-neutral, and use
that frame as a reference. Every real frame is then divided by it, pixel by
pixel, per channel. What is left is the scene as it would look under even light.

$$I_{corrected}(x,y,c) = I(x,y,c) \cdot \frac{\text{target}}{F(x,y,c)}$$

where $F$ is the flat field and `target` is its own mean, so overall brightness
is preserved.

## What a flat field needs to be

- **Evenly lit**: a diffuser over the lamp, or a white sheet across the box,
  lit the same way a real run is lit. The lamp's own position is part of what the
  correction must remove, so it has to be lit the same way the run was.
- **In focus and at the run's resolution** (3280×2464): a blurred flat field
  does not correct what the sensor actually saw.
- **Colour-neutral**: white or grey paper. If it is off-white, the correction
  would tint the result.
- **Smoothed**: the raw flat field carries sensor noise. Dividing by noise makes
  the noise worse. It must be blurred heavily (a wide Gaussian) so only the
  broad shading survives — that is the part being corrected.
- **Exposed in the middle of the range**: not clipped at 255, not in the noise
  floor. Either loses the ratio.

## Where the code should live

Three pieces, in order of how much they cost:

### 1. Capture the flat field (on the Pi, once)

A small addition to `app_v2.py`: a route that captures a flat frame with the
lamp on and saves it as `~/camweb/flat.jpeg` plus a note of the settings it was
taken with (`exposure_us`, `gain`, `colour_gains`, `lamp_brightness`). The
settings matter: correcting a run shot at different gains with a flat field from
another exposure is wrong.

### 2. Correct the frames (host-side, in the existing tool)

`scripts/stabilize-timelapse.sh` grows a `--flat <file>` option. The division is
an FFmpeg filter chain, and it must happen **before** the stabilisation step, so
the correction sees the same pixels the sensor produced:

```
[0:v][1:v]blend=all_mode=difference   # not right: this is a sum, not a ratio
```

FFmpeg has no native per-pixel divide for this purpose. Two honest options:

- **`--flat` in the script using `ffmpeg`'s `lut`/`blend` is fragile.** A ratio
  needs a division per pixel per channel, which `blend` does not offer.
- **Correct with a small Python step** using Pillow (already a dependency), or
  with `numpy` if it is available. A 3280×2464 frame divides in well under a
  second in numpy, and Pillow can do it with `ImageChops` only for subtraction,
  not division.

Recommendation: do it in Python with numpy, called from the script when `--flat`
is given, writing corrected frames to a `.flatfix/` subfolder so the originals
are never touched. If numpy is not present, the script says so and stops rather
than silently skipping the correction.

### 3. Or correct it in the app itself

The app could divide every captured frame by a stored flat field at capture time.
That is more code on the Pi and more work per frame, and it changes the record
(the frames are no longer what the sensor saw). Worth doing only if the gradient
has to be gone in the app itself, for the preview as well as the timelapse.

Recommendation: **keep it out of the capture path.** The frames are the record;
the correction belongs to the render, where it can be re-run with a better flat
field later.

## How to verify it worked

1. Measure the corrected frame's profile with the same eight-strip routine used
   in the investigation. The spread should fall from ~49 to single digits.
2. Compare a corrected frame against the flat field: their ratio should be flat.
3. Check that the corrected frame is not washed out at the edges — a flat field
   taken with the lamp in a different position over-corrects and leaves the
   corners bright.

A test belongs in `tests/`, on a synthetic frame with a known gradient: build a
flat field and a frame that is the flat field times a known scene, correct it,
and check the recovered scene matches. That is a stronger test than measuring the
real frames, whose true scene is unknown.

## Order of work

1. Capture a flat field on the Pi and look at it. **Do this first**: if the flat
   field does not show the same gradient, the whole approach is wrong and the
   reason has to be found before any code is written.
2. Write the correction and the synthetic test.
3. Re-render the 23 September session and compare the before and after profiles.
4. Only then consider whether the app should apply it at capture time.

## Unknowns to settle before coding

- Does the flat field, taken with the lamp on as a run uses it, carry the same
  gradient? Everything above depends on it.
- Is `numpy` acceptable as a dependency of the host-side tool, or should the
  correction be pure Pillow (slower, more code)?

## Facts about this Pi that the plan depends on

- **`lc.AwbModeEnum` has no `Manual`.** It exposes only `Auto`, `Cloudy`,
  `Custom`, `Daylight`, `Fluorescent`, `Incandescent`, `Indoor`, `Tungsten`. Any
  plan that assumes a manual white-balance mode has to allow for its absence —
  `camera.apply_controls` drops the control rather than leaving a mode that
  re-decides.
- **The lamp's brightness is software PWM at its best, and group `gpio` can write
  it.** The flat field has to be shot with the same lamp brightness a run uses,
  or the correction is measuring a different lighting setup.
- **A dark frame at the run's own settings carries no gradient.** At 6748 µs and
  gain 1.0 the dark-current spread works out to 0.04 levels. Any gradient in the
  corrected frames is therefore light, not sensor.
