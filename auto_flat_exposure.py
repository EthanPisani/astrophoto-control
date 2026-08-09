"""
auto_flat_exposure.py

Finds the correct flat-frame exposure time automatically: takes test
shots at increasing/decreasing exposures and bisects until the combined
R+G+B ("white") histogram peak lands at a target fraction of the full
sensor range (default 1/3 from the black point — bright enough for good
SNR, comfortably below the highlight roll-off/clipping).

This only touches exposure time. ISO, aperture, and everything else are
held fixed by the caller.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
import rawpy

log = logging.getLogger("auto_flat_exposure")


@dataclass
class FlatExposureResult:
    exposure_seconds: float
    peak_fraction: float                       # where the histogram peak landed, 0..1
    converged: bool
    iterations: int
    history: list[tuple[float, float]] = field(default_factory=list)  # (exposure_seconds, peak_fraction)


def measure_peak_fraction(nef_path: Path, bins: int = 512) -> float:
    """
    Loads a raw frame and returns the fractional position (0..1) of the
    peak of the combined R+G+B histogram across the full sensor range.

    half_size + no auto-brightening + linear gamma: we don't care about
    a pretty preview here, only a fast, exposure-proportional brightness
    reading. Auto-bright or camera-curve gamma would silently invalidate
    the whole calibration by rescaling brightness independent of the
    exposure we're trying to solve for.
    """
    with rawpy.imread(str(nef_path)) as raw:
        rgb = raw.postprocess(
            half_size=True,
            use_camera_wb=False,
            no_auto_bright=True,
            output_bps=16,
            gamma=(1, 1),
        )

    white = rgb.astype(np.float64).sum(axis=2)  # per-pixel R+G+B, 0 .. 3*65535
    max_value = 3 * 65535

    hist, edges = np.histogram(white, bins=bins, range=(0, max_value))
    peak_bin = int(np.argmax(hist))
    peak_value = (edges[peak_bin] + edges[peak_bin + 1]) / 2
    return float(peak_value / max_value)


def bisect_flat_exposure(
    capture_fn: Callable[[float], Path],
    target_fraction: float = 1 / 3,
    start_exposure: float = 1.0,
    tolerance: float = 0.02,
    max_iterations: int = 12,
    min_exposure: float = 1 / 4000,
    max_exposure: float = 30.0,
) -> FlatExposureResult:
    """
    Binary-searches exposure time so the R+G+B histogram peak lands at
    `target_fraction` of full range.

    capture_fn(exposure_seconds) must take one test shot at that
    exposure and return the path to the downloaded raw frame — plug in
    whatever capture pipeline you're using (see app.py's
    `_capture_single_test_frame` for the AstroCap wiring).

    Two phases:
      1. Exponential bracketing from start_exposure — double or halve
         the exposure until the target fraction is bracketed between a
         lo/hi pair (flat-panel brightness is fixed, so signal scales
         ~linearly with exposure time, meaning this converges fast).
      2. Classic bisection between lo/hi until within `tolerance` of the
         target or `max_iterations` is hit.
    """
    history: list[tuple[float, float]] = []

    def sample(exposure: float) -> float:
        exposure = max(min_exposure, min(max_exposure, exposure))
        frame_path = capture_fn(exposure)
        frac = measure_peak_fraction(frame_path)
        history.append((exposure, frac))
        log.info("flat calib: exposure=%.6fs -> peak_fraction=%.4f", exposure, frac)
        return frac

    exposure = start_exposure
    frac = sample(exposure)

    # ---- phase 1: bracket the target ----
    if frac < target_fraction:
        lo, hi = exposure, exposure
        hi_frac = frac
        while hi_frac < target_fraction and hi < max_exposure:
            lo = hi
            hi = min(max_exposure, hi * 2)
            hi_frac = sample(hi)
        exposure, frac = hi, hi_frac
    else:
        lo, hi = exposure, exposure
        lo_frac = frac
        while lo_frac > target_fraction and lo > min_exposure:
            hi = lo
            lo = max(min_exposure, lo / 2)
            lo_frac = sample(lo)
        exposure, frac = lo, lo_frac

    # ---- phase 2: bisect lo/hi ----
    converged = abs(frac - target_fraction) <= tolerance

    # Track discrete fraction values we've already seen during bisection.
    # When the exposure step becomes smaller than the measurement can
    # resolve (e.g. half_size downsampling quantises the histogram peak
    # into a handful of values), the bisection will oscillate forever
    # between the same two fractions.  We detect that stall and fall
    # back to the best result seen so far.
    bisect_fractions_seen: set[float] = {frac}
    bisect_stall_count = 0

    while not converged and len(history) < max_iterations and hi > lo:
        # If the interval has shrunk to a meaningless size relative to
        # the exposure value, further bisection just wastes test shots.
        mid = (lo + hi) / 2
        if mid > 0 and (hi - lo) / mid < 0.005:
            log.info("flat calib: bisection step too small (%.6f / %.6f), stopping",
                     hi - lo, mid)
            break

        mid_frac = sample(mid)
        exposure, frac = mid, mid_frac

        if abs(mid_frac - target_fraction) <= tolerance:
            converged = True
            break

        # Detect stall: we keep seeing the same discrete fractions
        # without making progress toward the target.
        if mid_frac in bisect_fractions_seen:
            bisect_stall_count += 1
            if bisect_stall_count >= 3:
                log.info("flat calib: measurement stalled (fraction resolution floor "
                         "reached — seen %.4f repeatedly)", mid_frac)
                break
        else:
            bisect_fractions_seen.add(mid_frac)
            bisect_stall_count = 0

        if mid_frac < target_fraction:
            lo = mid
        else:
            hi = mid

    # If we didn't converge, pick the single best (exposure, fraction)
    # pair from the full history — the one whose fraction is closest
    # to the target.
    if not converged and history:
        best = min(history, key=lambda item: abs(item[1] - target_fraction))
        exposure, frac = best
        # If even the best entry is within tolerance, call it converged.
        if abs(frac - target_fraction) <= tolerance:
            converged = True
        log.info("flat calib: best result picked from history: exposure=%.6fs "
                 "peak_fraction=%.4f (converged=%s)", exposure, frac, converged)

    return FlatExposureResult(
        exposure_seconds=exposure,
        peak_fraction=frac,
        converged=converged,
        iterations=len(history),
        history=history,
    )
