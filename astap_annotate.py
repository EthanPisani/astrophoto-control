"""
astap_annotate.py — astap_cli plate-solving + fully-offline annotation,
replacing local_annotate.py (twirl/photutils) as the solver and giving
siril_annotate.py-style annotations (DSO circles, constellation lines,
named-star labels) without any network dependency (no SIMBAD/astroquery,
no Siril/flatpak).

Drop-in goal: same input/output contract as
siril_annotate.py's run_flatpak_siril_annotation(config) -> (wcs_metadata,
output_path, dso_annotations) / local_annotate.py's run_astap_annotation(),
so callers (AstroCap's platesolve.py, the Flask task runner) only need to
swap the import:

    from astap_annotate import run_astap_annotation
    wcs_metadata, output_path, dso_annotations = run_astap_annotation({
        "input_nef": "/media/.../capture.nef",
        "output_image": "annotated.png",
        "focal_length": 135,
        "pixel_size": 3.91,
        "ra": "20:57:14",
        "dec": "43:55:09",
    })

--------------------------------------------------------------------------
WHAT THIS REPLACES, AND WHY
--------------------------------------------------------------------------
  local_annotate.py did              This file does
  ----------------------------------  ------------------------------------
  twirl asterism match + astropy WCS  astap_cli -- the compiled solver you
  fit (pure Python, slow)             confirmed is already fast and reliable
                                       on your setups; no re-implementation
                                       of star detection/matching needed.

  DSO CSVs (ngc/ic/messier)           Same CSVs, same priority/dedupe rules
                                       -- unchanged, still 100% local.

  (nothing -- annotation was a        Constellation line segments + named
  bare DSO-circle bake, no sky        bright-star labels, read from two
  context)                            local CSVs derived once from the HYG
                                       star database + Stellarium's western
                                       constellationship.fab (see "DATA
                                       FILES" below) -- this is the piece
                                       siril_annotate.py got via a live
                                       SIMBAD query per star name
                                       (_resolve_star_coord); here it's a
                                       flat lookup against a file on disk,
                                       so there is no online step at all,
                                       not even as an optional refinement.

--------------------------------------------------------------------------
DATA FILES (download once, keep local)
--------------------------------------------------------------------------
Two new CSVs are needed alongside your existing ngc.csv/ic.csv/messier.csv.
Both are included with this delivery, built from public data:

  known_stars.csv
      Every named star in the HYG v4.1 database (astronexus/HYG-Database)
      at magnitude <= 6.5 (naked-eye limit) -- 358 rows. Columns: name,
      hip, bayer, flam, con, ra (deg), dec (deg), mag. Source:
      https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv
      (CC BY-SA 4.0).

  constellation_lines.csv
      Every line segment in Stellarium's default "western" sky culture,
      with each endpoint already resolved to RA/Dec via the same HYG
      database (so no HIP-number lookup is needed at solve time; this file
      is fully self-contained). Columns: constellation, hip1, ra1, dec1,
      hip2, ra2, dec2 -- 674 segments across 87 constellations. Source:
      https://raw.githubusercontent.com/Stellarium/stellarium (western
      skyculture constellationship.fab, GPL-2.0 -- the line-shape data
      itself, per the Stellarium project, is not separately re-licensable;
      keep the file local/internal to AstroCap rather than redistributing
      it standalone).

Default location expected by this script (mirrors where astap_rest.py
already looks for the DSO catalogues):
    ~/.local_annotate/dso-catalogs/{ngc,ic,messier}.csv      (existing)
    ~/.local_annotate/star-catalogs/known_stars.csv          (new)
    ~/.local_annotate/star-catalogs/constellation_lines.csv  (new)

Override either directory with ASTAP_DSO_CATALOG_DIR / ASTAP_STAR_CATALOG_DIR
if you'd rather keep them somewhere else (e.g. next to the Siril-derived
CSVs you already have under ~/src/sc-data/catalogs/siril).

--------------------------------------------------------------------------
ONE THING TO VERIFY AGAINST YOUR astap_cli BUILD
--------------------------------------------------------------------------
astap_cli's stdout format for "how many stars did you match" isn't
consistent across releases, and I don't have your binary to check its
exact wording. `_parse_star_counts()` below tries a handful of regexes
that match the strings ASTAP has used historically ("stars aligned",
"stars matched", "Nr stars used"); if none hit, n_stars_detected/matched
fall back to 0 rather than guessing. Run `astap_cli -f <file> ... ` once
by hand, check the real stdout, and adjust the regex list if it's
reporting 0 for you -- everything else in this file doesn't depend on
that number (it's metadata only, matching the field local_annotate.py's
callers already read: result.n_stars_detected / n_stars_matched).

--------------------------------------------------------------------------
REQUIREMENTS
--------------------------------------------------------------------------
    astropy, numpy, rawpy, opencv-python (cv2), Pillow
    astap_cli on PATH (or set ASTAP_PATH)

    pip install astropy numpy rawpy opencv-python Pillow --break-system-packages

No astroquery, no matplotlib, no Siril/flatpak, no network egress at
solve time -- everything above is either bundled with this delivery or
already on disk from your DSO-catalogue setup.
"""

from __future__ import annotations

import csv
import functools
import logging
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import rawpy
import cv2
from astropy.io import fits
from astropy.wcs import WCS
from astropy.wcs.utils import proj_plane_pixel_scales
from astropy.coordinates import Angle, SkyCoord
import astropy.units as u
from PIL import Image, ImageDraw, ImageFont
import threading
import uuid
import requests
import time

log = logging.getLogger("astap_annotate")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ASTAP_PATH = os.environ.get("ASTAP_PATH", "astap_cli")
ASTAP_TIMEOUT_SEC = int(os.environ.get("ASTAP_TIMEOUT_SEC", "30"))
ASTAP_SEARCH_RADIUS_DEG = float(os.environ.get("ASTAP_SEARCH_RADIUS_DEG", "15.0"))
ASTAP_BLIND_TIMEOUT_SEC = int(os.environ.get("ASTAP_BLIND_TIMEOUT_SEC", "8"))
# "north" | "south" | "auto" — narrows the blind-solve search to one
# hemisphere for speed. "auto" does a true all-sky search (slower).
ASTAP_HEMISPHERE = os.environ.get("ASTAP_HEMISPHERE", "north").strip().lower()

DSO_CATALOG_DIR = Path(os.path.expanduser(
    os.environ.get("ASTAP_DSO_CATALOG_DIR", "~/.local_annotate/dso-catalogs")
))
STAR_CATALOG_DIR = Path(os.path.expanduser(
    os.environ.get("ASTAP_STAR_CATALOG_DIR", "~/.local_annotate/star-catalogs")
))

_DSO_CATALOG_FILES: list[tuple[str, str]] = [
    ("messier.csv", "messier"),
    ("ngc.csv", "ngc"),
    ("ic.csv", "ic"),
]
_DATA_DIR = Path(os.environ.get("LOCAL_ANNOTATE_DATA_DIR", "~/.local_annotate")).expanduser()
_GAIA_DB_PATH = Path(os.environ.get("LOCAL_ANNOTATE_GAIA_DB", str(_DATA_DIR / "gaia.sqlite3")))
_DSO_CATALOG_DIR = Path(os.environ.get(
    "LOCAL_ANNOTATE_DSO_CATALOG_DIR",
    os.environ.get("ASTROCAP_DSO_CATALOG_DIR", str(_DATA_DIR / "dso-catalogs")),
))

TEMP_DIR = Path(os.environ.get("ASTAP_TEMP_DIR", "./astro_temp"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)
# ---------------------------------------------------------------------------
# Task-layer configuration (mirrors platesolve.py / siril_annotate.py so the
# Flask task runner in app.py can drive this module identically)
# ---------------------------------------------------------------------------

GPHOTO_API_BASE = os.environ.get("GPHOTO_API_BASE", "http://localhost:8080").rstrip("/")
HTTP_TIMEOUT = int(os.environ.get("ASTROCAP_HTTP_TIMEOUT", 20))
OUTPUT_BASE_DIR = Path(os.environ.get("ASTROCAP_OUTDIR", "./captures"))
PLATESOLVE_DIR = OUTPUT_BASE_DIR / "platesolve"

# Default optics for the local solve path. These are only used when the
# caller doesn't supply explicit values (the Flask endpoints pass them
# through from the UI); they match the defaults used by the Siril backend.
_DEFAULT_FOCAL_LENGTH_MM = 135.0
_DEFAULT_PIXEL_SIZE_UM = 3.91
# Colors matching siril_annotate.py (RGB order for PIL)
_CATALOG_COLOR = {
    "messier": (255, 210, 63),   # gold #FFD23F
    "ngc": (124, 255, 107),      # green #7CFF6B
    "ic": (255, 179, 71),        # orange #FFB347
    "star": (255, 93, 115),      # pink/red #FF5D73 (for stars)
    "constellation_line": (141, 232, 255),  # cyan #8DE8FF
    "constellation_label": (183, 241, 255), # light cyan #B7F1FF
    "star_label": (255, 224, 138),          # warm yellow #FFE08A
    "target_circle": (255, 40, 40),          # bright red for in-frame target
    "target_arrow": (255, 50, 50),           # red for off-screen arrow
}

# Minimum angular diameter (arcmin) for a DSO target to be "big enough"
# that we recolor its existing circle/label red instead of drawing a new
# crosshair. Smaller targets always get the crosshair so you can find them.
_TARGET_DSO_MIN_DIAMETER_ARCMIN = 3.0

# Same priority rule as siril_annotate.py: Messier > NGC > IC > named star >
# unnamed constellation-line vertex. Lower number = higher priority = kept
# when two entries land on (near enough to be) the same physical object.
_CATALOG_PRIORITY = {"messier": 0, "ngc": 1, "ic": 2, "star": 3, "constellation": 4}


def _catalog_priority(catalog_kind: str) -> int:
    return _CATALOG_PRIORITY.get((catalog_kind or "").strip().lower(), _CATALOG_PRIORITY["constellation"])


# ---------------------------------------------------------------------------
# Angular distance helper
# ---------------------------------------------------------------------------

def _angular_separation_arcsec(ra1_deg: Optional[float], dec1_deg: Optional[float],
                               ra2_deg: Optional[float], dec2_deg: Optional[float]) -> float:
    """Angular distance between two sky positions in arcseconds.
    Returns -1 if either point is None."""
    if ra1_deg is None or dec1_deg is None or ra2_deg is None or dec2_deg is None:
        return -1.0
    try:
        c1 = SkyCoord(ra=ra1_deg, dec=dec1_deg, unit=u.deg)
        c2 = SkyCoord(ra=ra2_deg, dec=dec2_deg, unit=u.deg)
        return float(c1.separation(c2).arcsecond)
    except Exception:
        return -1.0


# ---------------------------------------------------------------------------
# Data structures (mirrors siril_annotate.py's DsoAnnotation so downstream
# code -- get_task_result(), _run_astap_pipeline(), etc. -- doesn't have to
# change how it reads the return value)
# ---------------------------------------------------------------------------

@dataclass
class DsoAnnotation:
    name: str
    obj_type: str            # "Gx" (Messier/NGC/IC) or "star"
    ra: float
    dec: float
    catalog_kind: str = ""   # ngc | ic | messier | star | constellation
    pixel_x: float = 0.0
    pixel_y: float = 0.0
    diameter_arcmin: float = 0.0


@dataclass
class _ConstellationSegment:
    constellation: str
    hip1: int
    ra1: float
    dec1: float
    hip2: int
    ra2: float
    dec2: float


# ---------------------------------------------------------------------------
# Catalog loading (all local disk reads, cached for the life of the process)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def load_dso_catalog() -> list[dict[str, Any]]:
    """Messier/NGC/IC catalogue, handling missing sizes gracefully with logging."""
    catalog: list[dict[str, Any]] = []
    
    for filename, kind in _DSO_CATALOG_FILES:
        path = DSO_CATALOG_DIR / filename
        if not path.exists():
            log.info("DSO catalogue not found, skipping: %s", path)
            continue
            
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    name = row["name"].strip()
                    # Safely extract diameter, defaulting to 0.0 if blank
                    raw_diameter = row.get("diameter", "").strip()
                    diameter_val = float(raw_diameter) if raw_diameter else 0.0
                    
                    # --- MISSING DATA RESCUE ---
                    # If the CSV has 0.0 for the size, give it a visible fallback size
                    # if diameter_val <= 0.0:
                    #     if kind == "ic":
                    #         diameter_val = 100.0  # Draw missing ICs as 10-arcmin circles
                    #     elif kind == "ngc":
                    #         diameter_val = 5.0   # Draw missing NGCs as 5-arcmin circles
                    #     else:
                    #         diameter_val = 15.0  # Draw missing Messiers as 15-arcmin circles
                            
                        # # Log the fallback action
                        # print(
                        #     "Missing diameter for %s (%s). Applied fallback size: %s arcmin", 
                        #     name, kind.upper(), diameter_val
                        # )
                    # ---------------------------

                    catalog.append({
                        "name": name,
                        "type": kind,
                        "ra": float(row["ra"]),
                        "dec": float(row["dec"]),
                        "diameter": diameter_val,
                    })
                except (ValueError, KeyError):
                    continue
                    
    log.info("Loaded %d DSO catalogue entries from %s", len(catalog), DSO_CATALOG_DIR)
    return catalog

@functools.lru_cache(maxsize=1)
def load_known_stars() -> list[dict[str, Any]]:
    """Named bright stars (mag <= 6.5) from known_stars.csv (HYG-derived)."""
    path = STAR_CATALOG_DIR / "known_stars.csv"
    if not path.exists():
        log.warning("known_stars.csv not found at %s -- star labels disabled", path)
        return []
    stars: list[dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                stars.append({
                    "name": row["name"].strip(),
                    "con": row.get("con", "").strip(),
                    "ra": float(row["ra"]),
                    "dec": float(row["dec"]),
                    "mag": float(row["mag"]) if row.get("mag") else 99.0,
                })
            except (ValueError, KeyError):
                continue
    log.info("Loaded %d named stars from %s", len(stars), path)
    return stars


@functools.lru_cache(maxsize=1)
def load_constellation_lines() -> list[_ConstellationSegment]:
    """Pre-resolved constellation line segments (constellation_lines.csv)."""
    path = STAR_CATALOG_DIR / "constellation_lines.csv"
    if not path.exists():
        log.warning("constellation_lines.csv not found at %s -- constellation overlay disabled", path)
        return []
    segments: list[_ConstellationSegment] = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                segments.append(_ConstellationSegment(
                    constellation=row["constellation"].strip(),
                    hip1=int(row["hip1"]),
                    ra1=float(row["ra1"]), dec1=float(row["dec1"]),
                    hip2=int(row["hip2"]),
                    ra2=float(row["ra2"]), dec2=float(row["dec2"]),
                ))
            except (ValueError, KeyError):
                continue
    log.info("Loaded %d constellation line segments from %s", len(segments), path)
    return segments


# ---------------------------------------------------------------------------
# RAW -> FITS
# ---------------------------------------------------------------------------
#
# Orientation note: earlier versions of this file called
# raw.postprocess(..., user_flip=0), which *forces* rawpy to ignore the
# RAW file's own orientation tag. Nikon (and most other) sensors are read
# out in a fixed physical order that generally doesn't match "how the
# photographer held the camera" -- normally rawpy/dcraw correct for this
# automatically using the orientation metadata embedded in the NEF
# (user_flip=-1, the default). Forcing user_flip=0 disabled that
# correction, so the decoded array came out upside-down (and/or mirrored,
# depending on the body) before any of our own WCS/annotation code ever
# ran -- this is what produced the "very close, but upside down" result.
# Fix: don't pass user_flip at all, so rawpy uses the RAW's own metadata
# (equivalent to the default user_flip=-1).
#
# Also: this file no longer flips the array for "FITS bottom-left origin"
# convention. That convention only matters if you hand the FITS off to a
# tool that assumes it (DS9, Siril, etc.); astap_cli doesn't care what
# row order you give it -- it just fits a WCS to whatever pixel grid it's
# given. Since nothing downstream of astap_cli in this file expects FITS
# convention either (we render our own PNG with PIL), skipping that flip
# means the array we solve on, the array we render on, and the WCS pixel
# coordinates astap_cli hands back are all in the exact same index space
# -- no flip/flip-back bookkeeping, and nothing left to get backwards.

def _decode_raw(raw_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Decode a NEF (or other rawpy-supported RAW) once, returning
    (gray_for_solving, rgb_for_display) at identical dimensions and
    orientation (both un-flipped -- see note above)."""
    with rawpy.imread(raw_path) as raw:
        rgb16 = raw.postprocess(
            half_size=True,
            output_bps=16,
            no_auto_bright=True,
            use_camera_wb=True,
            output_color=rawpy.ColorSpace.sRGB,
        )
    gray = cv2.cvtColor(rgb16, cv2.COLOR_RGB2GRAY)
    return gray, rgb16


# File extensions that rawpy can handle (RAW camera formats).
_RAW_EXTENSIONS = {'.nef', '.cr2', '.arw', '.dng', '.orf', '.rw2', '.raf',
                   '.pef', '.srw', '.3fr', '.dcr', '.kdc', '.mrw', '.nrw',
                   '.raw', '.rwl', '.srf', '.x3f'}

# Image formats we can load directly with PIL/OpenCV (non-RAW).
_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp',
                     '.webp', '.gif', '.ppm', '.pgm'}


def _is_raw(path: str) -> bool:
    return Path(path).suffix.lower() in _RAW_EXTENSIONS


def _is_image(path: str) -> bool:
    return Path(path).suffix.lower() in _IMAGE_EXTENSIONS


def _load_image(image_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Load a standard image (PNG, JPEG, TIFF, …) and return
    (gray_for_solving, rgb_for_display) — same contract as _decode_raw."""
    # PIL handles orientation, colour profiles, and bit depth automatically.
    pil_img = Image.open(image_path)
    # Convert to RGB (handles greyscale, RGBA, palette images)
    if pil_img.mode in ('RGBA', 'LA', 'P'):
        pil_img = pil_img.convert('RGBA').convert('RGB')
    elif pil_img.mode not in ('RGB', 'L'):
        pil_img = pil_img.convert('RGB')

    rgb = np.asarray(pil_img)
    # If loaded as greyscale, expand to 3-channel for uniform handling
    if rgb.ndim == 2:
        rgb = np.stack([rgb, rgb, rgb], axis=-1)
    elif rgb.shape[2] == 4:  # RGBA
        rgb = rgb[:, :, :3]

    # 8-bit images — scale to 16-bit range so downstream code
    # (bake_png_annotations) sees consistent brightness.
    if rgb.dtype == np.uint8:
        rgb = rgb.astype(np.uint16) * 257  # 8→16 bit: multiply by 257

    gray = cv2.cvtColor(rgb.astype(np.uint16), cv2.COLOR_RGB2GRAY)
    return gray, rgb


def _decode_input(input_path: str) -> tuple[np.ndarray, np.ndarray]:
    """Load any supported image (RAW or standard format), returning
    (gray_for_solving, rgb_for_display)."""
    if _is_raw(input_path):
        return _decode_raw(input_path)
    if _is_image(input_path):
        return _load_image(input_path)
    # Fallback: try rawpy first, then PIL
    try:
        return _decode_raw(input_path)
    except Exception:
        return _load_image(input_path)


def decode_raw_to_fits(raw_path: str, fits_path: str) -> tuple[int, int]:
    """Decode a NEF (or other rawpy-supported RAW) to a mono FITS for ASTAP.
    Returns (height, width) of the decoded image."""
    gray, _rgb16 = _decode_raw(raw_path)
    hdu = fits.PrimaryHDU(gray)
    fits.HDUList([hdu]).writeto(fits_path, overwrite=True)
    return gray.shape  # (height, width)


# ---------------------------------------------------------------------------
# astap_cli solving
# ---------------------------------------------------------------------------

def _parse_ra_dec(ra: Any, dec: Any) -> tuple[Optional[float], Optional[float]]:
    """Accept "HH:MM:SS" / "DD:MM:SS" strings (siril_annotate.py's config
    format) or plain floats already in degrees."""
    if ra is None or dec is None:
        return None, None
    try:
        if isinstance(ra, str) and ":" in ra:
            ra_deg = Angle(ra, unit=u.hourangle).degree
        else:
            ra_deg = float(ra)
        if isinstance(dec, str) and ":" in dec:
            dec_deg = Angle(dec, unit=u.deg).degree
        else:
            dec_deg = float(dec)
        return ra_deg, dec_deg
    except Exception as exc:
        log.warning("Could not parse ra/dec hint (%r, %r): %s", ra, dec, exc)
        return None, None


def _estimate_fov_deg(focal_length_mm: float, pixel_size_um: float, width_px: int) -> Optional[float]:
    """Rough field-of-width in degrees, used only as an ASTAP solve hint
    (-fov) to narrow/speed up the search; not used for anything else since
    the real scale comes back from the solved WCS."""
    if not focal_length_mm or not pixel_size_um or not width_px:
        return None
    arcsec_per_px = 206265.0 * (pixel_size_um * 1e-6 * 1000.0) / focal_length_mm  # pixel_size in microns -> mm
    return (arcsec_per_px * width_px) / 3600.0


_STAR_COUNT_PATTERNS = [
    re.compile(r"(\d+)\s+stars?\s+aligned", re.IGNORECASE),
    re.compile(r"(\d+)\s+stars?\s+matched", re.IGNORECASE),
    re.compile(r"nr\s+stars?\s+used\D+(\d+)", re.IGNORECASE),
    re.compile(r"stars\s+detected\D+(\d+)", re.IGNORECASE),
]
_STAR_DETECT_PATTERNS = [
    re.compile(r"(\d+)\s+stars?\s+extracted", re.IGNORECASE),
    re.compile(r"(\d+)\s+stars?\s+detected", re.IGNORECASE),
]


def _parse_star_counts(astap_stdout: str) -> tuple[int, int]:
    """Best-effort parse of astap_cli's stdout for detected/matched star
    counts. See the module docstring -- verify these patterns against your
    actual astap_cli output and adjust if they come back 0."""
    n_detected = 0
    n_matched = 0
    for pat in _STAR_DETECT_PATTERNS:
        m = pat.search(astap_stdout)
        if m:
            n_detected = int(m.group(1))
            break
    for pat in _STAR_COUNT_PATTERNS:
        m = pat.search(astap_stdout)
        if m:
            n_matched = int(m.group(1))
            break
    return n_detected, n_matched


def run_astap_solver(fits_path: str, hint_ra_deg: Optional[float], hint_dec_deg: Optional[float],
                      fov_deg: Optional[float] = None,
                      radius_deg: float = ASTAP_SEARCH_RADIUS_DEG,
                      blind: bool = False,
                      hemisphere: str = "north",
                      timeout_sec: int = ASTAP_TIMEOUT_SEC) -> tuple[bool, str]:
    """Runs astap_cli. Returns (solved, combined_stdout_stderr).

    When blind=True, uses the G05 database (optimised for wide-field blind
    searching), no -ra/-spd position hint, and a wide search radius.
    hemisphere ("north"/"south"/"auto") narrows the blind search:
    "north" uses -spd 135 (Dec=+45° centre), "south" uses -spd 45 (Dec=-45°),
    "auto" uses -r 180 with no centre (true all-sky).

    When blind=False, uses the D50 database (deeper, more precise) with
    the full position hint for a fast targeted solve.
    """
    if blind:
        # G05 database — designed for wide-field blind searching, fewer
        # stars per square degree means faster quad matching over large areas.
        # spd = dec + 90° (south pole distance). Northern mid-lat: spd=135.
        if hemisphere == "north":
            cmd = [ASTAP_PATH, "-f", fits_path, "-r", "90", "-spd", "135", "-w", "-z", "0", "-D", "G05"]
        elif hemisphere == "south":
            cmd = [ASTAP_PATH, "-f", fits_path, "-r", "90", "-spd", "45", "-w", "-z", "0", "-D", "G05"]
        else:
            # True all-sky blind: no -ra/-spd, max radius, G05 for speed
            cmd = [ASTAP_PATH, "-f", fits_path, "-r", "180", "-w", "-z", "0", "-D", "G05"]
    else:
        # D50 database — deeper star catalogue for precise targeted solves
        cmd = [ASTAP_PATH, "-f", fits_path, "-r", str(radius_deg), "-w", "-z", "0", "-D", "D50"]

    if fov_deg:
        cmd.extend(["-fov", f"{fov_deg:.4f}"])

    if not blind and hint_ra_deg is not None and hint_dec_deg is not None:
        cmd.extend(["-ra", str(hint_ra_deg / 15.0), "-spd", str(hint_dec_deg + 90.0)])

    log.info("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, timeout=timeout_sec)
        output = (result.stdout or "") + (result.stderr or "")
    except subprocess.TimeoutExpired as exc:
        log.error("astap_cli timed out after %ss", timeout_sec)
        return False, str(exc)

    wcs_file = fits_path.replace(".fits", ".wcs")
    return os.path.exists(wcs_file), output


def _solve_blind_then_hinted(fits_path: str, hint_ra_deg: Optional[float],
                               hint_dec_deg: Optional[float],
                               fov_deg: Optional[float] = None,
                               hemisphere: str = "north") -> tuple[bool, str]:
    """Try fast blind solve; fall back to hinted solve on failure."""
    # Stage 1: blind solve (fast, no position hint required)
    log.info("Stage 1: blind solve (hemisphere=%s) on %s", hemisphere, fits_path)
    solved, output = run_astap_solver(
        fits_path, None, None, fov_deg=fov_deg,
        blind=True, hemisphere=hemisphere,
        timeout_sec=ASTAP_BLIND_TIMEOUT_SEC,
    )
    if solved:
        log.info("Blind solve succeeded in stage 1")
        return True, output

    log.info("Blind solve failed — falling back to hinted solve")
    # Stage 2: hinted solve with target coordinates
    if hint_ra_deg is not None and hint_dec_deg is not None:
        return run_astap_solver(fits_path, hint_ra_deg, hint_dec_deg, fov_deg=fov_deg)

    return False, output


# ---------------------------------------------------------------------------
# WCS metadata + object mapping
# ---------------------------------------------------------------------------

def _load_wcs(fits_path: str) -> WCS:
    wcs_file = fits_path.replace(".fits", ".wcs")
    with open(wcs_file, "r") as f:
        header = fits.Header.fromstring(f.read(), sep="\n")
    return WCS(header), header


def build_wcs_metadata(fits_path: str, img_shape: tuple[int, int],
                        n_stars_detected: int, n_stars_matched: int) -> dict[str, Any]:
    """Center RA/Dec, rotation, plate scale, field size -- matches the keys
    local_annotate.py's callers already read off wcs_metadata."""
    height, width = img_shape
    wcs_obj, header = _load_wcs(fits_path)

    try:
        pixel_scale_deg = proj_plane_pixel_scales(wcs_obj)[0]
    except Exception:
        pixel_scale_deg = abs(float(header.get("CD1_1", 0.0033)))
    pixel_scale_arcmin = pixel_scale_deg * 60.0
    scale_arcsec_per_px = pixel_scale_arcmin * 60.0

    center_coord = wcs_obj.pixel_to_world(width / 2, height / 2)
    rotation_deg = math.degrees(math.atan2(header.get("PC2_1", 0.0), header.get("PC1_1", 1.0)))

    return {
        "ra_deg": float(center_coord.ra.degree),
        "dec_deg": float(center_coord.dec.degree),
        "rotation": float(rotation_deg),
        "scale": float(scale_arcsec_per_px),
        "field_width_arcmin": float(pixel_scale_arcmin * width),
        "field_height_arcmin": float(pixel_scale_arcmin * height),
        "n_stars_detected": int(n_stars_detected),
        "n_stars_matched": int(n_stars_matched),
        "pixel_scale_arcmin": float(pixel_scale_arcmin),  # internal, used by the annotator below
    }


def _map_dso_objects(wcs_obj: WCS, width: int, height: int, pixel_scale_arcmin: float) -> list[DsoAnnotation]:
    out: list[DsoAnnotation] = []
    catalog = load_dso_catalog()
    in_frame = 0
    for item in catalog:
        px, py = wcs_obj.world_to_pixel_values(item["ra"], item["dec"])
        if not (0 <= px <= width and 0 <= py <= height):
            continue
        in_frame += 1
        diameter_arcmin = item.get("diameter", 0.0)
        out.append(DsoAnnotation(
            name=item["name"], obj_type="Gx", ra=item["ra"], dec=item["dec"],
            catalog_kind=item["type"], pixel_x=float(px), pixel_y=float(py),
            diameter_arcmin=diameter_arcmin,
        ))
    log.info("DSO mapping: %d loaded, %d in frame", len(catalog), in_frame)
    return out


def _map_known_stars(wcs_obj: WCS, width: int, height: int, margin: float = 40.0) -> list[DsoAnnotation]:
    out: list[DsoAnnotation] = []
    stars = load_known_stars()
    in_frame = 0
    for star in stars:
        px, py = wcs_obj.world_to_pixel_values(star["ra"], star["dec"])
        if not (-margin <= px <= width + margin and -margin <= py <= height + margin):
            continue
        in_frame += 1
        out.append(DsoAnnotation(
            name=star["name"], obj_type="star", ra=star["ra"], dec=star["dec"],
            catalog_kind="star", pixel_x=float(px), pixel_y=float(py),
        ))
    log.info("Known stars: %d loaded, %d in frame", len(stars), in_frame)
    return out


def _find_star_name(ra_deg: float, dec_deg: float, tolerance_deg: float = 0.02) -> Optional[str]:
    """Look up a named star by RA/Dec in the known stars catalogue."""
    for star in load_known_stars():
        if abs(star["ra"] - ra_deg) < tolerance_deg and abs(star["dec"] - dec_deg) < tolerance_deg:
            return star["name"]
    return None


def _map_constellation_lines(wcs_obj: WCS, width: int, height: int,
                              margin: float = 80.0) -> tuple[list[tuple[str, tuple[float, float], tuple[float, float]]], list[DsoAnnotation]]:
    """Returns (constellation_line_segments, star_annotations) for every segment with at
    least one endpoint inside the frame (+ margin), matching how
    siril_annotate.py drew partially-visible asterisms at the frame edge."""
    visible_segments: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    star_annotations: list[DsoAnnotation] = []
    seen_stars = set()  # Track by (ra, dec) to avoid duplicates
    
    for seg in load_constellation_lines():
        x1, y1 = wcs_obj.world_to_pixel_values(seg.ra1, seg.dec1)
        x2, y2 = wcs_obj.world_to_pixel_values(seg.ra2, seg.dec2)
        in1 = -margin <= x1 <= width + margin and -margin <= y1 <= height + margin
        in2 = -margin <= x2 <= width + margin and -margin <= y2 <= height + margin
        if in1 or in2:
            visible_segments.append((seg.constellation, (float(x1), float(y1)), (float(x2), float(y2))))
            
            # Add star annotations for endpoints that are in frame
            for ra, dec, x, y in [(seg.ra1, seg.dec1, x1, y1), (seg.ra2, seg.dec2, x2, y2)]:
                key = (round(ra, 6), round(dec, 6))
                if key in seen_stars:
                    continue
                seen_stars.add(key)
                if -margin <= x <= width + margin and -margin <= y <= height + margin:
                    # Find the star name from known_stars
                    star_name = _find_star_name(ra, dec)
                    if star_name:
                        star_annotations.append(DsoAnnotation(
                            name=star_name, obj_type="star", ra=ra, dec=dec,
                            catalog_kind="star", pixel_x=float(x), pixel_y=float(y),
                        ))
    
    log.info("Constellation lines: %d segments visible, %d endpoint stars labeled",
             len(visible_segments), len(star_annotations))
    return visible_segments, star_annotations


def _dedupe_by_priority(annotations: list[DsoAnnotation], sep_px: float = 45.0) -> list[DsoAnnotation]:
    """Same idea as siril_annotate.py's _dedupe_dso_by_priority, done in
    pixel space (we already know the frame's pixel scale, no need for
    SkyCoord separations here).

    Only deduplicates objects of the SAME catalog_kind.  A star (e.g. Sadr)
    sitting on top of a DSO (e.g. IC 1318, the Gamma Cygni Nebula) are
    different objects and both should appear; the old cross-type dedup was
    silently eating named stars that happened to be co-located with a DSO
    catalogue entry.
    """
    ordered = sorted(annotations, key=lambda a: _catalog_priority(a.catalog_kind))
    kept: list[DsoAnnotation] = []
    for ann in ordered:
        # only compare against kept entries of the SAME catalog_kind
        if any(
            k.catalog_kind == ann.catalog_kind
            and math.hypot(ann.pixel_x - k.pixel_x, ann.pixel_y - k.pixel_y) < sep_px
            for k in kept
        ):
            continue
        kept.append(ann)
    return kept


def map_and_annotate(fits_path: str, img_shape: tuple[int, int], pixel_scale_arcmin: float) -> tuple[list[DsoAnnotation], list[tuple[str, tuple[float, float], tuple[float, float]]]]:
    height, width = img_shape
    wcs_obj, _ = _load_wcs(fits_path)

    dso = _map_dso_objects(wcs_obj, width, height, pixel_scale_arcmin)
    stars = _map_known_stars(wcs_obj, width, height)
    constellation_segments, constellation_stars = _map_constellation_lines(wcs_obj, width, height)
    
    # Combine all annotations and deduplicate
    all_annotations = _dedupe_by_priority(dso + stars + constellation_stars)
    log.info("Annotation totals: DSO=%d stars=%d constellation=%d → after dedup=%d",
             len(dso), len(stars), len(constellation_stars), len(all_annotations))
    return all_annotations, constellation_segments


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if os.path.exists(candidate):
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _stretch_to_8bit_rgb(rgb16: np.ndarray) -> np.ndarray:
    """Siril-style RAW-to-display stretch: neutralize the green cast from
    the sensor's Bayer filter, then a black-point/white-point linear
    stretch + gamma, matching siril_annotate.py's bake_png_annotations so
    the two backends look the same. Input is the 16-bit RGB straight off
    rawpy.postprocess(); output is 8-bit RGB ready for PIL."""
    rgb = rgb16.astype(np.float32) / 65535.0

    bg = [np.median(rgb[:, :, c]) for c in range(3)]
    for c in range(3):
        if bg[c] > 1e-6:
            rgb[:, :, c] /= bg[c]

    peak = float(np.max(rgb))
    if peak > 1e-6:
        rgb = np.clip(rgb / peak, 0, 1)

    black_point, white_point = 0.05, 0.85
    rgb = np.clip((rgb - black_point) / (white_point - black_point), 0, 1)
    rgb = np.power(rgb, 0.5)

    return (rgb * 255.0).astype(np.uint8)


def _draw_target_indicator(draw: ImageDraw.ImageDraw, width: int, height: int,
                           target_ra_deg: float, target_dec_deg: float,
                           target_name: str,
                           target_offset_arcsec: float,
                           border_offset_x: int, border_offset_y: int) -> None:
    """Draw a red circle if the target is in the middle 3/4 of the frame,
    or a large red arrow at the frame edge pointing toward the target if
    it lies outside the frame.  Also renders the angular offset below
    the label (e.g. \"2.3°\" or \"145\\\"\").

    Uses _pixel_scale_arcmin_cache[0] and the WCS from the most recent
    solve (accessed via _cached_wcs_path global set in bake_png_annotations).
    """
    import math as _math

    # Load WCS from the cached path set by bake_png_annotations
    wcs_path = _cached_wcs_path
    if not wcs_path or not os.path.exists(wcs_path):
        log.debug("No WCS available for target indicator")
        return

    try:
        wcs_obj, _header = _load_wcs(wcs_path)
        tx, ty = wcs_obj.world_to_pixel_values(target_ra_deg, target_dec_deg)
    except Exception:
        log.debug("Failed to project target coords to pixels", exc_info=True)
        return

    # Adjust for border
    tx += border_offset_x
    ty += border_offset_y

    # Middle 3/4 bounds (1/8 margin on each side)
    margin_x = width / 8.0
    margin_y = height / 8.0
    in_middle = (margin_x <= tx <= width - margin_x and
                 margin_y <= ty <= height - margin_y)

    circle_color = _CATALOG_COLOR["target_circle"] + (220,)  # ~0.86 alpha
    arrow_color = _CATALOG_COLOR["target_arrow"] + (230,)
    font = _load_font(42)
    small_font = _load_font(28)

    # Format angular offset as human-readable text
    offset_text = ""
    if target_offset_arcsec > 0:
        if target_offset_arcsec >= 3600.0:
            offset_text = f"{target_offset_arcsec / 3600.0:.1f}°"
        elif target_offset_arcsec >= 60.0:
            offset_text = f"{target_offset_arcsec / 60.0:.1f}'"
        else:
            offset_text = f"{target_offset_arcsec:.0f}\""

    if in_middle:
        # --- Target on screen: draw large red circle ---
        radius = min(width, height) * 0.06  # 6% of frame size
        radius = max(radius, 30.0)
        draw.ellipse(
            [tx - radius, ty - radius, tx + radius, ty + radius],
            outline=circle_color, width=5,
        )
        # Crosshair
        cross = radius * 0.4
        draw.line([(tx - cross, ty), (tx + cross, ty)], fill=circle_color, width=3)
        draw.line([(tx, ty - cross), (tx, ty + cross)], fill=circle_color, width=3)
        # Label below
        label = target_name if target_name else "TARGET"
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((tx - tw / 2, ty + radius + 8), label, fill=circle_color, font=font)
        # Offset below label
        if offset_text:
            obox = draw.textbbox((0, 0), offset_text, font=small_font)
            otw = obox[2] - obox[0]
            draw.text((tx - otw / 2, ty + radius + 8 + bbox[3] - bbox[1] + 4),
                      offset_text, fill=circle_color, font=small_font)
        log.info("Target '%s' in frame at pixel (%.0f, %.0f) — circle drawn", label, tx, ty)
    else:
        # --- Target off screen: draw large arrow at edge ---
        cx, cy = width / 2.0, height / 2.0
        dx = tx - cx
        dy = ty - cy
        dist = _math.hypot(dx, dy)
        if dist < 1.0:
            return  # degenerate: target at centre

        # Intersect the ray (cx,cy)→(tx,ty) with the frame bounding box
        # (with a small inset so the arrow sits inside, not on the edge)
        inset = 60.0
        edge_x, edge_y = _intersect_ray_with_rect(
            cx, cy, dx / dist, dy / dist,
            inset, inset, width - inset, height - inset,
        )

        # Draw a bold arrow pointing toward the target
        arrow_len = min(width, height) * 0.12
        arrow_len = max(arrow_len, 60.0)
        # Unit vector toward target
        ux, uy = dx / dist, dy / dist
        # Arrow tip is at edge_x, edge_y; base is back along the ray
        base_x = edge_x - ux * arrow_len
        base_y = edge_y - uy * arrow_len

        # Arrow head
        head_len = arrow_len * 0.35
        head_width = arrow_len * 0.25
        # Perpendicular to ray
        px, py = -uy, ux

        # Draw thick arrow shaft
        draw.line([(base_x, base_y), (edge_x, edge_y)], fill=arrow_color, width=7)

        # Arrow head triangle
        tip = (edge_x, edge_y)
        left = (base_x + px * head_width, base_y + py * head_width)
        right = (base_x - px * head_width, base_y - py * head_width)
        draw.polygon([tip, left, right], fill=arrow_color)

        # Label near arrow
        label = target_name if target_name else "TARGET"
        label_x = edge_x - ux * (arrow_len + 45)
        label_y = edge_y - uy * (arrow_len + 45)
        bbox = draw.textbbox((0, 0), label, font=font)
        tw = bbox[2] - bbox[0]
        draw.text((label_x - tw / 2, label_y - 21), label, fill=arrow_color, font=font)
        # Offset below label
        if offset_text:
            obox = draw.textbbox((0, 0), offset_text, font=small_font)
            otw = obox[2] - obox[0]
            draw.text((label_x - otw / 2, label_y - 21 + bbox[3] - bbox[1] + 4),
                      offset_text, fill=arrow_color, font=small_font)

        angle_deg = _math.degrees(_math.atan2(dy, dx))
        log.info("Target '%s' off-screen at bearing %.0f° — arrow drawn at (%.0f, %.0f)",
                 label, angle_deg, edge_x, edge_y)


def _intersect_ray_with_rect(cx: float, cy: float, ux: float, uy: float,
                              x0: float, y0: float, x1: float, y1: float) -> tuple[float, float]:
    """Find where a ray from (cx,cy) in direction (ux,uy) exits rect [x0,x1]×[y0,y1].
    Returns the intersection point on the rect boundary."""
    t_min = float("inf")
    # Left edge (x = x0)
    if ux < -1e-9:
        t = (x0 - cx) / ux
        if t > 0:
            y = cy + t * uy
            if y0 <= y <= y1:
                t_min = min(t_min, t)
    # Right edge (x = x1)
    if ux > 1e-9:
        t = (x1 - cx) / ux
        if t > 0:
            y = cy + t * uy
            if y0 <= y <= y1:
                t_min = min(t_min, t)
    # Top edge (y = y0)
    if uy < -1e-9:
        t = (y0 - cy) / uy
        if t > 0:
            x = cx + t * ux
            if x0 <= x <= x1:
                t_min = min(t_min, t)
    # Bottom edge (y = y1)
    if uy > 1e-9:
        t = (y1 - cy) / uy
        if t > 0:
            x = cx + t * ux
            if x0 <= x <= x1:
                t_min = min(t_min, t)
    if t_min == float("inf"):
        return cx, cy
    return cx + ux * t_min, cy + uy * t_min


# Cache for WCS path so _draw_target_indicator can use it
_cached_wcs_path: str = ""


def bake_png_annotations(rgb16: np.ndarray, annotations: list[DsoAnnotation],
                          constellation_segments: list[tuple[str, tuple[float, float], tuple[float, float]]],
                          output_path: str,
                          target_ra_deg: Optional[float] = None,
                          target_dec_deg: Optional[float] = None,
                          target_name: str = "",
                          target_offset_arcsec: float = -1.0,
                          fits_path_hint: str = "") -> str:
    """PIL-based bake matching siril_annotate.py's visual style:
      - constellation lines (cyan, thin, alpha ~0.65), drawn first
      - DSO circles with actual angular diameter, color-coded per catalogue
      - named-star markers (small pink dots) + labels (warm yellow)
      - target indicator: red circle (if in middle 3/4) or red arrow (if off-screen)

    rgb16 is the *un-flipped* array from _decode_raw() -- the same array
    (same shape, same orientation) that decode_raw_to_fits() wrote to
    FITS and astap_cli solved against, so annotation pixel_x/pixel_y
    (straight from wcs_obj.world_to_pixel_values) can be drawn directly
    with no coordinate flip of any kind. See the orientation note above
    decode_raw_to_fits().
    """
    global _cached_wcs_path
    # Try to locate the WCS file from the FITS path hint
    if fits_path_hint:
        _cached_wcs_path = fits_path_hint.replace(".fits", ".wcs")

    img_8bit = _stretch_to_8bit_rgb(rgb16)
    base_height, base_width = img_8bit.shape[:2]

    # Add a black border (padding) around the image, matching siril_annotate.py
    # Border is 5% of image dimensions with a minimum of 24 pixels
    border_px_x = max(24, int(round(base_width * 0.05)))
    border_px_y = max(24, int(round(base_height * 0.05)))
    img_8bit = np.pad(
        img_8bit,
        ((border_px_y, border_px_y), (border_px_x, border_px_x), (0, 0)),
        mode="constant",
        constant_values=0.0,
    )
    height, width = img_8bit.shape[:2]

    base = Image.fromarray(img_8bit).convert("RGBA")
    draw = ImageDraw.Draw(base, "RGBA")

    # Get pixel scale for diameter calculations
    pixel_scale_arcmin = _pixel_scale_arcmin_cache[0]
    pixel_scale_deg = pixel_scale_arcmin / 60.0
    arcsec_per_pixel = pixel_scale_deg * 3600.0

    # Adjust annotation coordinates for the border offset
    border_offset_x = border_px_x
    border_offset_y = border_px_y

    # 1. Constellation lines (bottom layer) - cyan, thin, semi-transparent
    line_color = _CATALOG_COLOR["constellation_line"] + (166,)  # ~0.65 alpha
    for _constellation, (x1, y1), (x2, y2) in constellation_segments:
        draw.line([(x1 + border_offset_x, y1 + border_offset_y), (x2 + border_offset_x, y2 + border_offset_y)], fill=line_color, width=2)

    # 2. DSO circles + named stars from annotations (on top)
    # Sort by catalog priority so higher priority (Messier) draws on top
    star_color = _CATALOG_COLOR["star"] + (242,)  # ~0.95 alpha
    star_label_color = _CATALOG_COLOR["star_label"] + (230,)
    label_font = _load_font(38)
    small_font = _load_font(30)

    sorted_annotations = sorted(annotations, key=lambda a: _catalog_priority(a.catalog_kind))

    # ---- Target-DSO matching ----
    # If the target matches a DSO in our annotations AND that DSO is large
    # enough, we recolor its existing circle/label red instead of drawing
    # a new crosshair. Small/unmatched targets still get the crosshair.
    _target_annotation_index: int | None = None
    if target_name:
        target_key = target_name.strip().lower().replace(" ", "")
        for i, ann in enumerate(sorted_annotations):
            ann_key = ann.name.strip().lower().replace(" ", "")
            if ann_key == target_key and ann.catalog_kind != "star":
                if ann.diameter_arcmin >= _TARGET_DSO_MIN_DIAMETER_ARCMIN:
                    _target_annotation_index = i
                break
    # ----

    # Overlap avoidance: track placed label bounding boxes
    placed_labels: list[tuple[float, float, float, float]] = []  # (x0, y0, x1, y1)

    def _label_bbox(tx: float, ty: float, text: str, font: ImageFont.FreeTypeFont) -> tuple[float, float, float, float]:
        """Return (x0, y0, x1, y1) of text drawn at (tx, ty)."""
        bb = draw.textbbox((tx, ty), text, font=font)
        return (bb[0], bb[1], bb[2], bb[3])

    def _overlap_area(b0: tuple[float, float, float, float],
                       b1: tuple[float, float, float, float]) -> float:
        """Intersection area of two bboxes, 0 if no overlap."""
        ox0 = max(b0[0], b1[0])
        oy0 = max(b0[1], b1[1])
        ox1 = min(b0[2], b1[2])
        oy1 = min(b0[3], b1[3])
        if ox0 < ox1 and oy0 < oy1:
            return (ox1 - ox0) * (oy1 - oy0)
        return 0.0

    def _pick_label_position(cx: float, cy: float, radius: float, text: str,
                              font: ImageFont.FreeTypeFont) -> tuple[float, float, float, float]:
        """Choose best label position around a circle, returning
        (text_x, text_y, tick_x, tick_y) where:
          - (text_x, text_y) = top-left of label text
          - (tick_x, tick_y) = point on circle edge the tick line connects to
        """
        offset = 30  # pixels from circle edge to label
        tw, th = _label_bbox(0, 0, text, font=font)[2:]  # width/height only (bbox at origin)

        # Candidates: (text_x, text_y, tick_x, tick_y) — label top-left + where tick
        # attaches to the circle edge (anchor point on circle the tick draws toward)
        candidates: list[tuple[float, float, float, float]] = [
            # Right of circle
            (cx + radius + offset, cy - th / 2,          cx + radius, cy),
            # Above-right (45°)
            (cx + radius * 0.7 + offset, cy - radius * 0.7 - th, cx + radius * 0.707, cy - radius * 0.707),
            # Below-right (-45°)
            (cx + radius * 0.7 + offset, cy + radius * 0.7,      cx + radius * 0.707, cy + radius * 0.707),
            # Above
            (cx - tw / 2,           cy - radius - offset - th,   cx, cy - radius),
            # Below
            (cx - tw / 2,           cy + radius + offset,         cx, cy + radius),
        ]

        best_pos = candidates[0]
        best_overlap = float("inf")

        for (tx, ty, tix, tiy) in candidates:
            bbox = _label_bbox(tx, ty, text, font=font)
            total_overlap = sum(_overlap_area(bbox, placed) for placed in placed_labels)
            if total_overlap < best_overlap:
                best_overlap = total_overlap
                best_pos = (tx, ty, tix, tiy)
            if total_overlap == 0.0:
                break  # perfect, no need to check more

        tx, ty, tix, tiy = best_pos
        return tx, ty, tix, tiy

    for ann_idx, ann in enumerate(sorted_annotations):
        x = ann.pixel_x + border_offset_x
        y = ann.pixel_y + border_offset_y
        kind = (ann.catalog_kind or "").lower()
        is_target_dso = (_target_annotation_index is not None and ann_idx == _target_annotation_index)
        color = _CATALOG_COLOR.get(kind, (255, 255, 255))

        if kind == "star":
            # Named star - small pink dot with warm yellow label
            r = 3
            draw.ellipse([x - r, y - r, x + r, y + r], fill=star_color)
            text_x, text_y, tick_x, tick_y = _pick_label_position(
                x, y, r, ann.name, small_font,
            )
            draw.text((text_x, text_y), ann.name, fill=star_label_color, font=small_font)
            draw.line([(text_x, text_y + 7), (tick_x, tick_y)], fill=star_label_color, width=4)
            sb = _label_bbox(text_x, text_y, ann.name, font=small_font)
            placed_labels.append((sb[0], sb[1], sb[2], sb[3]))
            continue

        # DSO object - circle with actual angular diameter
        if ann.diameter_arcmin > 0:
            diameter_arcsec = ann.diameter_arcmin * 60.0
            radius_px = (diameter_arcsec / arcsec_per_pixel) / 2.0
            min_radius = 4.0 if kind == "ic" else 5.0
            radius_px = max(radius_px, min_radius)
        else:
            radius_px = 4.0 if kind == "ic" else 5.0

        radius_px = min(radius_px, max(width, height) * 5.0)

        # If this DSO is the target, use red target colors
        if is_target_dso:
            outline_color = _CATALOG_COLOR["target_circle"] + (242,)
            label_fill = _CATALOG_COLOR["target_circle"] + (242,)
            line_width = 6  # bolder for the target
        else:
            outline_color = color + (242,)
            label_fill = outline_color
            line_width = 4

        # Draw circle outline
        draw.ellipse([x - radius_px, y - radius_px, x + radius_px, y + radius_px],
                        outline=outline_color, width=line_width)

        # Label with overlap avoidance + tick line
        label = ann.name
        text_x, text_y, tick_x, tick_y = _pick_label_position(
            x, y, radius_px, label, label_font,
        )
        draw.text((text_x, text_y), label, fill=label_fill, font=label_font)

        # Tick line
        bb = _label_bbox(text_x, text_y, label, font=label_font)
        label_cx = (bb[0] + bb[2]) / 2.0
        label_cy = (bb[1] + bb[3]) / 2.0
        dx_c = x - label_cx
        dy_c = y - label_cy
        if abs(dx_c) > abs(dy_c):
            tick_start_x = bb[2] if dx_c > 0 else bb[0]
            tick_start_y = label_cy + dy_c * (tick_start_x - label_cx) / dx_c if dx_c != 0 else label_cy
        else:
            tick_start_y = bb[3] if dy_c > 0 else bb[1]
            tick_start_x = label_cx + dx_c * (tick_start_y - label_cy) / dy_c if dy_c != 0 else label_cx

        # Clamp to label bbox
        tick_start_x = max(bb[0], min(bb[2], tick_start_x))
        tick_start_y = max(bb[1], min(bb[3], tick_start_y))

        draw.line([(tick_start_x, tick_start_y), (tick_x, tick_y)], fill=outline_color, width=4)

        placed_labels.append((bb[0], bb[1], bb[2], bb[3]))

    # 3. Target indicator — only for small/unmatched targets.
    # Large DSOs that matched the target were already recolored red above.
    if (target_ra_deg is not None and target_dec_deg is not None
            and _target_annotation_index is None):
        _draw_target_indicator(
            draw, width, height,
            target_ra_deg, target_dec_deg, target_name,
            target_offset_arcsec,
            border_offset_x, border_offset_y,
        )

    # Convert back to RGB and save
    base = base.convert("RGB")
    base.save(output_path)
    return output_path


# _pixel_scale_arcmin_cache is a one-element list used as a mutable cell so
# bake_png_annotations (called after mapping) can see the frame's plate
# scale without threading an extra parameter through every call site that
# already exists in callers modeled on local_annotate.py/siril_annotate.py.
_pixel_scale_arcmin_cache = [0.0033 * 60.0]

# ---------------------------------------------------------------------------
# Data structures (field-for-field match with siril_annotate.py's DsoAnnotation
# so downstream consumers of `dso_annotations` need no changes)
# ---------------------------------------------------------------------------


@dataclass
class PlateSolveResult:
    """Task result — field-for-field match with platesolve.py / siril_annotate.py
    so app.py's /platesolve/api/status endpoint needs no changes."""
    success: bool = False
    task_id: str = ""
    status: str = "pending"       # pending | capturing | solving | done | error
    error: str = ""
    capture_id: str = ""
    local_path: str = ""
    target_name: str = ""
    target_ra: float = 0.0
    target_dec: float = 0.0
    solved_ra: float = 0.0
    solved_dec: float = 0.0
    solved_rotation: float = 0.0
    solved_scale: float = 0.0      # arcsec / pixel
    field_width_arcmin: float = 0.0
    field_height_arcmin: float = 0.0
    n_stars_detected: int = 0
    n_stars_matched: int = 0
    annotations: list[DsoAnnotation] = field(default_factory=list)
    annotated_path: str = ""
    wcs_json: str = ""            # serialized WCS for front-end overlay


# In-memory task store
_tasks: dict[str, PlateSolveResult] = {}
_tasks_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Task management (identical contract to platesolve.py / siril_annotate.py)
# ---------------------------------------------------------------------------

def create_task() -> str:
    task_id = str(uuid.uuid4())
    with _tasks_lock:
        _tasks[task_id] = PlateSolveResult(task_id=task_id, status="pending")
    return task_id


def _get_task(task_id: str) -> PlateSolveResult:
    with _tasks_lock:
        return _tasks[task_id]


def _update_task(task_id: str, **kwargs: Any) -> None:
    with _tasks_lock:
        result = _tasks[task_id]
        for k, v in kwargs.items():
            setattr(result, k, v)


def get_task_result(task_id: str) -> Optional[dict[str, Any]]:
    with _tasks_lock:
        result = _tasks.get(task_id)
        if result is None:
            return None
        return {
            "success": result.success,
            "task_id": result.task_id,
            "status": result.status,
            "error": result.error,
            "capture_id": result.capture_id,
            "local_path": result.local_path,
            "target_name": result.target_name,
            "target_ra": result.target_ra,
            "target_dec": result.target_dec,
            "solved_ra": result.solved_ra,
            "solved_dec": result.solved_dec,
            "solved_rotation": result.solved_rotation,
            "solved_scale": result.solved_scale,
            "field_width_arcmin": result.field_width_arcmin,
            "field_height_arcmin": result.field_height_arcmin,
            "n_stars_detected": result.n_stars_detected,
            "n_stars_matched": result.n_stars_matched,
            "annotations": [
                {
                    "name": a.name,
                    "type": a.obj_type,
                    "ra": a.ra,
                    "dec": a.dec,
                    "diameter_arcmin": a.diameter_arcmin,
                }
                for a in result.annotations
            ],
            "annotated_path": result.annotated_path,
            "wcs_json": result.wcs_json,
        }


# ---------------------------------------------------------------------------
# Helper: gphoto2 API client (lightweight, no session tracking) — mirrors
# platesolve.py so the capture step behaves identically.
# ---------------------------------------------------------------------------

def _api_post(path: str, body: dict[str, Any] | None = None,
              timeout: int = HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"{GPHOTO_API_BASE}{path}"
    resp = requests.post(url, json=body, timeout=max(timeout, 60))
    if not resp.ok:
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text}
        msg = payload.get("message") or payload.get("error") or f"HTTP {resp.status_code}"
        raise RuntimeError(msg)
    if not resp.content:
        return {}
    return resp.json()


def _api_get(path: str, timeout: int = HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"{GPHOTO_API_BASE}{path}"
    resp = requests.get(url, timeout=timeout)
    if not resp.ok:
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text}
        msg = payload.get("message") or payload.get("error") or f"HTTP {resp.status_code}"
        raise RuntimeError(msg)
    if not resp.content:
        return {}
    return resp.json()


def _download_file(path: str, dest: Path, timeout: int = 120) -> None:
    url = f"{GPHOTO_API_BASE}{path}"
    with requests.get(url, stream=True, timeout=timeout) as resp:
        if not resp.ok:
            try:
                payload = resp.json()
            except ValueError:
                payload = {"error": resp.text}
            msg = payload.get("message") or payload.get("error") or f"HTTP {resp.status_code}"
            raise RuntimeError(msg)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("wb") as fh:
            for chunk in resp.iter_content(1024 * 1024):
                if chunk:
                    fh.write(chunk)

# ---------------------------------------------------------------------------
# Top-level entry point -- same contract as run_flatpak_siril_annotation /
# run_astap_annotation
# ---------------------------------------------------------------------------

def run_astap_annotation(config: dict[str, Any]) -> tuple[Optional[dict[str, Any]], Optional[str], list[DsoAnnotation]]:
    """
    config keys (all required, same as local_annotate.py/siril_annotate.py):
        input_nef, output_image, focal_length, pixel_size, ra, dec

    Optional keys:
        hemisphere: "north" | "south" | "auto" (default "north")
        target_ra_deg, target_dec_deg: target coords for arrow/circle overlay

    Returns (wcs_metadata, output_path, dso_annotations) -- wcs_metadata is
    None and output_path is None on failure (matches the "if wcs_metadata is
    None or output_path is None: raise" check already in AstroCap's
    pipeline runners).
    """
    input_nef = config["input_nef"]
    output_image = config["output_image"]
    focal_length_mm = float(config.get("focal_length", 135.0))
    pixel_size_um = float(config.get("pixel_size", 3.91))
    hemisphere = str(config.get("hemisphere", ASTAP_HEMISPHERE) or "north").strip().lower()

    # Target coordinates for arrow/circle overlay (optional)
    target_ra_deg = config.get("target_ra_deg")
    target_dec_deg = config.get("target_dec_deg")

    session_id = Path(input_nef).stem
    fits_path = str(TEMP_DIR / f"{session_id}.fits")

    try:
        gray, rgb16 = _decode_input(input_nef)
        img_shape = gray.shape
        fits.HDUList([fits.PrimaryHDU(gray)]).writeto(fits_path, overwrite=True)
    except Exception:
        log.exception("Image decode failed for %s", input_nef)
        return None, None, []

    hint_ra_deg, hint_dec_deg = _parse_ra_dec(config.get("ra"), config.get("dec"))
    fov_deg = _estimate_fov_deg(focal_length_mm, pixel_size_um, img_shape[1])

    # --- Two-stage solve: blind first, then hinted fallback ---
    solved, astap_output = _solve_blind_then_hinted(
        fits_path, hint_ra_deg, hint_dec_deg, fov_deg=fov_deg, hemisphere=hemisphere,
    )
    if not solved:
        log.error("astap_cli failed to solve %s\n--- astap_cli output ---\n%s", input_nef, astap_output)
        return None, None, []

    n_detected, n_matched = _parse_star_counts(astap_output)
    wcs_metadata = build_wcs_metadata(fits_path, img_shape, n_detected, n_matched)
    _pixel_scale_arcmin_cache[0] = wcs_metadata["pixel_scale_arcmin"]

    annotations, constellation_segments = map_and_annotate(fits_path, img_shape, wcs_metadata["pixel_scale_arcmin"])

    try:
        offset_arcsec = _angular_separation_arcsec(
            wcs_metadata.get("ra_deg"), wcs_metadata.get("dec_deg"),
            target_ra_deg, target_dec_deg,
        )
        output_path = bake_png_annotations(
            rgb16, annotations, constellation_segments, output_image,
            target_ra_deg=target_ra_deg, target_dec_deg=target_dec_deg,
            target_name=str(config.get("target_name", "") or ""),
            target_offset_arcsec=offset_arcsec,
            fits_path_hint=fits_path,
        )
    except Exception:
        log.exception("Annotation bake failed for %s", input_nef)
        return None, None, []

    return wcs_metadata, output_path, annotations



def _run_astap_pipeline(
    task_id: str,
    target_name: str,
    exposure_seconds: int = 30,
    iso: str = "400",
    search_radius_deg: float = 1.5,
    focal_length_mm: float = _DEFAULT_FOCAL_LENGTH_MM,
    pixel_size_um: float = _DEFAULT_PIXEL_SIZE_UM,
    hemisphere: str = "north",
) -> None:
    """Full pipeline in a background thread: resolve target → capture one
    frame → download → local plate-solve + annotate → store result."""
    result = _get_task(task_id)

    try:
        # --- Step 1: Resolve target ---
        _update_task(task_id, status="solving", progress="Resolving target…")
        try:
            ra, dec, resolved_name = resolve_target(target_name)
            result.target_name = resolved_name
            result.target_ra = ra
            result.target_dec = dec
            log.info("Target resolved: %s at (%.4f, %.4f)", resolved_name, ra, dec)
        except ValueError as exc:
            log.warning("Target not resolved: %s — solving without target overlay", exc)
            ra = dec = resolved_name = None
            result.target_name = target_name  # keep user's input as-is

        # --- Step 2: Capture ---
        _update_task(task_id, status="capturing", progress="Capturing frame…")
        capture_body = {
            "shutter_speed": "bulb",
            "exposure_seconds": exposure_seconds,
            "iso": str(iso),
            "capture_target": "sdram",
        }
        accepted = _api_post("/api/v1/captures", body=capture_body)
        capture_id = str(accepted.get("capture_id") or "")
        if not capture_id:
            raise RuntimeError("Backend did not return capture_id")
        result.capture_id = capture_id

        deadline = time.time() + max(90, exposure_seconds * 2 + 60)
        record: dict[str, Any] = {}
        while time.time() < deadline:
            record = _api_get(f"/api/v1/captures/{capture_id}")
            state = record.get("status")
            if state in ("complete", "done"):
                break
            if state in ("failed", "error"):
                raise RuntimeError(record.get("error", f"Capture failed with status {state}"))
            time.sleep(2)
        else:
            raise RuntimeError("Capture timed out")

        # --- Step 3: Download ---
        _update_task(task_id, status="solving", progress="Downloading image…")
        captures_dir = PLATESOLVE_DIR / task_id
        captures_dir.mkdir(parents=True, exist_ok=True)
        source_name = record.get("source_name", f"{capture_id}.nef")
        local_name = f"capture_{source_name}"
        local_path = captures_dir / local_name
        _download_file(f"/api/v1/captures/{capture_id}/file", local_path)
        result.local_path = str(local_path)

        try:
            _api_post(f"/api/v1/captures/{capture_id}/downloaded")
        except Exception:
            pass

        # --- Step 4: Local plate-solve + annotate ---
        _update_task(task_id, status="solving", progress="Plate solving…")
        wcs_metadata, output_path, dso_annotations = run_astap_annotation({
            "input_nef": str(local_path),
            "output_image": "annotated.png",
            "focal_length": focal_length_mm,
            "pixel_size": pixel_size_um,
            "ra": f"{ra}" if ra is not None else "",
            "dec": f"{dec}" if dec is not None else "",
            "hemisphere": hemisphere,
            "target_ra_deg": ra,
            "target_dec_deg": dec,
            "target_name": str(resolved_name or ""),
        })
        if wcs_metadata is None or output_path is None:
            raise RuntimeError("Local annotation pipeline failed")

        # --- Step 5: Propagate solved metadata ---
        result.annotated_path = str(output_path)
        result.solved_ra = float(wcs_metadata.get("ra_deg", 0.0))
        result.solved_dec = float(wcs_metadata.get("dec_deg", 0.0))
        result.solved_rotation = float(wcs_metadata.get("rotation", 0.0))
        result.solved_scale = float(wcs_metadata.get("scale", 0.0))
        result.field_width_arcmin = float(wcs_metadata.get("field_width_arcmin", 0.0))
        result.field_height_arcmin = float(wcs_metadata.get("field_height_arcmin", 0.0))
        result.n_stars_detected = int(wcs_metadata.get("n_stars_detected", 0))
        result.n_stars_matched = int(wcs_metadata.get("n_stars_matched", 0))
        result.annotations = dso_annotations
        result.wcs_json = "{}"
        result.target_offset_arcsec = _angular_separation_arcsec(
            result.solved_ra, result.solved_dec, ra, dec,
        )

        result.success = True
        result.status = "done"
        log.info(
            "Local plate solve complete: center=(%.4f, %.4f) scale=%.3f\"/px "
            "rotation=%.2f field=%.1f'x%.1f' stars=%d/%d objects=%d offset=%.0f\"",
            result.solved_ra, result.solved_dec, result.solved_scale,
            result.solved_rotation, result.field_width_arcmin, result.field_height_arcmin,
            result.n_stars_matched, result.n_stars_detected, len(result.annotations),
            result.target_offset_arcsec,
        )

    except Exception as exc:
        log.exception("Local plate solve failed")
        error_msg = f"{type(exc).__name__}: {exc}" if str(exc) else f"{type(exc).__name__} (no message -- see server log for traceback)"
        result.status = "error"
        result.error = error_msg
        _update_task(task_id, status="error", error=error_msg)



_DSO_CATALOG_FILES: list[tuple[str, str]] = [
    ("messier.csv", "messier"),
    ("ngc.csv", "ngc"),
    ("ic.csv", "ic"),
]

def _catalogue_kind_for_name(catalog_kind: str, raw_name: str) -> str:
    catalog_kind = (catalog_kind or "").strip().lower()
    compact = (raw_name or "").strip().replace(" ", "")
    if re.match(r"^(?:M|MESSIER)(\d+[A-Za-z]?)$", compact, re.IGNORECASE):
        return "messier"
    if re.match(r"^(?:IC|I)(\d+[A-Za-z]?)$", compact, re.IGNORECASE):
        return "ic"
    if re.match(r"^(?:NGC|N)(\d+[A-Za-z]?)$", compact, re.IGNORECASE):
        return "ngc"
    return catalog_kind


@functools.lru_cache(maxsize=1)
def _load_dso_catalogues_csv() -> tuple[dict[str, Any], ...]:
    objects: list[dict[str, Any]] = []
    if not _DSO_CATALOG_DIR.exists():
        log.warning(
            "DSO catalogue directory not found: %s -- annotation will find zero objects "
            "in every field until this exists. Fix: create the directory (or point "
            "LOCAL_ANNOTATE_DSO_CATALOG_DIR / ASTROCAP_DSO_CATALOG_DIR at one that "
            "does) and populate it with messier.csv, ngc.csv, ic.csv (columns: "
            "name,ra,dec,diameter -- ra/dec in decimal degrees, diameter in arcmin). "
            "OpenNGC (https://github.com/mattiaverga/OpenNGC, CC-BY-SA-4.0) is a good "
            "free source for NGC/IC/Messier positions to build these from.",
            _DSO_CATALOG_DIR,
        )
        return tuple(objects)

    import csv
    for filename, catalog_kind in _DSO_CATALOG_FILES:
        path = _DSO_CATALOG_DIR / filename
        if not path.exists():
            log.warning(
                "Catalogue file missing, skipping: %s (annotation will be missing all "
                "%s objects until this file exists)", path, catalog_kind,
            )
            continue
        with path.open("r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    name = (row.get("name") or "").strip()
                    if not name:
                        continue
                    ra_deg = float(row["ra"])
                    dec_deg = float(row["dec"])
                except (KeyError, ValueError, TypeError):
                    continue
                try:
                    diameter_arcmin = float((row.get("diameter") or "0").strip() or 0.0)
                except ValueError:
                    diameter_arcmin = 0.0
                objects.append({
                    "name": name,
                    "ra": ra_deg, "dec": dec_deg,
                    "diameter_arcmin": diameter_arcmin,
                    "catalog_kind": _catalogue_kind_for_name(catalog_kind, name),
                })
    log.info("DSO catalogue ready: %d objects from %s", len(objects), _DSO_CATALOG_DIR)
    return tuple(objects)

def _normalise_dso_key(raw: str) -> Optional[tuple[str, str]]:
    """Return (catalogue_prefix, normalised_number) from a user-typed DSO name.

    Normalisation strips leading zeros from the numeric part so that
    "ngc218", "NGC0218", "NGC 218", "ngc 0218" all map to ("ngc", "218").
    The catalogue prefix is one of {"ngc", "ic", "m"}.

    Returns None if the input doesn't look like a DSO identifier.
    """
    compact = raw.strip().replace(" ", "")
    if not compact:
        return None
    m = re.match(
        r"^(?:(?:NGC|N)|(?:IC|I)|(?:M(?:ESSIER)?))(\d+[A-Za-z]?)$",
        compact, re.IGNORECASE,
    )
    if not m:
        return None
    num = m.group(1)  # e.g. "0218" or "1" or "101a"
    # Determine prefix
    upper = compact.upper()
    if upper.startswith("NGC") or upper.startswith("N"):
        prefix = "ngc"
    elif upper.startswith("IC") or upper.startswith("I"):
        prefix = "ic"
    else:
        prefix = "m"
    # Strip leading zeros from the numeric part (but keep a trailing letter)
    num_stripped = re.sub(r"^0+", "", num)
    if not num_stripped:  # degenerate: "NGC0000"
        return None
    return prefix, num_stripped.lower()


def _resolve_from_local_catalogue(target_name: str) -> Optional[tuple[float, float, str]]:
    """Look up a target by exact name or alias in the local DSO CSVs.

    Handles zero-padded catalogue names (NGC0218 → ngc218) and common
    user input variants (case variations, with/without spaces).
    """
    # 1. Try normalised DSO key match (handles zero-padded NGC/IC numbers)
    norm = _normalise_dso_key(target_name)
    if norm is not None:
        target_prefix, target_num = norm
        for obj in _load_dso_catalogues_csv():
            obj_norm = _normalise_dso_key(obj["name"])
            if obj_norm is not None:
                obj_prefix, obj_num = obj_norm
                if obj_prefix == target_prefix and obj_num == target_num:
                    return obj["ra"], obj["dec"], obj["name"]

    # 2. Fallback: simple string match (handles Messier "M1", "M 1", etc.)
    key = target_name.strip().lower()
    key_compact = key.replace(" ", "")
    for obj in _load_dso_catalogues_csv():
        obj_lower = obj["name"].strip().lower()
        candidates = {obj_lower, obj_lower.replace(" ", "")}
        if key in candidates or key_compact in candidates:
            return obj["ra"], obj["dec"], obj["name"]

    return None


def resolve_target(target_name: str) -> tuple[float, float, str]:
    """Resolve a target name to (ra_deg, dec_deg, resolved_name).

    Priority: SIMBAD (online) → local DSO catalogue CSVs (offline). Mirrors
    platesolve.py's resolve_target so the local backend behaves the same.
    """

    match = _resolve_from_local_catalogue(target_name)
    if match is not None:
        return match

    raise ValueError(
        f"Cannot resolve target name: {target_name}. SIMBAD is unavailable "
        "or could not resolve it, and no offline match was found."
    )


def _run_astap_from_file(
    task_id: str,
    file_path: str,
    target_name: str,
    search_radius_deg: float = 1.5,
    focal_length_mm: float = _DEFAULT_FOCAL_LENGTH_MM,
    pixel_size_um: float = _DEFAULT_PIXEL_SIZE_UM,
    hemisphere: str = "north",
) -> None:
    """Pipeline that starts from an existing image file instead of capturing
    live (uploaded file path)."""
    result = _get_task(task_id)

    try:
        local_path = Path(file_path)
        if not local_path.exists():
            raise RuntimeError(f"File not found: {file_path}")
        result.local_path = str(local_path)

        # --- Step 1: Resolve target ---
        _update_task(task_id, status="solving", progress="Resolving target…")
        try:
            ra, dec, resolved_name = resolve_target(target_name)
            result.target_name = resolved_name
            result.target_ra = ra
            result.target_dec = dec
            log.info("Target resolved: %s at (%.4f, %.4f)", resolved_name, ra, dec)
        except ValueError as exc:
            log.warning("Target not resolved: %s — solving without target overlay", exc)
            ra = dec = resolved_name = None
            result.target_name = target_name  # keep user's input as-is

        # --- Step 2: Local plate-solve + annotate ---
        _update_task(task_id, status="solving", progress="Plate solving…")
        captures_dir = PLATESOLVE_DIR / task_id
        captures_dir.mkdir(parents=True, exist_ok=True)
        output_path = captures_dir / "annotated.png"
        wcs_metadata, output_path_str, dso_annotations = run_astap_annotation({
            "input_nef": str(local_path),
            "output_image": str(output_path),
            "focal_length": focal_length_mm,
            "pixel_size": pixel_size_um,
            "ra": f"{ra}" if ra is not None else "",
            "dec": f"{dec}" if dec is not None else "",
            "hemisphere": hemisphere,
            "target_ra_deg": ra,
            "target_dec_deg": dec,
            "target_name": str(resolved_name or ""),
        })
        if wcs_metadata is None or output_path_str is None:
            raise RuntimeError("Local annotation pipeline failed")

        # --- Step 3: Propagate solved metadata ---
        result.annotated_path = str(output_path_str)
        result.solved_ra = float(wcs_metadata.get("ra_deg", 0.0))
        result.solved_dec = float(wcs_metadata.get("dec_deg", 0.0))
        result.solved_rotation = float(wcs_metadata.get("rotation", 0.0))
        result.solved_scale = float(wcs_metadata.get("scale", 0.0))
        result.field_width_arcmin = float(wcs_metadata.get("field_width_arcmin", 0.0))
        result.field_height_arcmin = float(wcs_metadata.get("field_height_arcmin", 0.0))
        result.n_stars_detected = int(wcs_metadata.get("n_stars_detected", 0))
        result.n_stars_matched = int(wcs_metadata.get("n_stars_matched", 0))
        result.annotations = dso_annotations
        result.wcs_json = "{}"
        result.target_offset_arcsec = _angular_separation_arcsec(
            result.solved_ra, result.solved_dec, ra, dec,
        )

        result.success = True
        result.status = "done"
        log.info(
            "Local plate solve from file complete: center=(%.4f, %.4f) scale=%.3f\"/px "
            "rotation=%.2f field=%.1f'x%.1f' stars=%d/%d objects=%d offset=%.0f\"",
            result.solved_ra, result.solved_dec, result.solved_scale,
            result.solved_rotation, result.field_width_arcmin, result.field_height_arcmin,
            result.n_stars_matched, result.n_stars_detected, len(result.annotations),
            result.target_offset_arcsec,
        )

    except Exception as exc:
        log.exception("Local plate solve from file failed")
        error_msg = f"{type(exc).__name__}: {exc}" if str(exc) else f"{type(exc).__name__} (no message -- see server log for traceback)"
        result.status = "error"
        result.error = error_msg
        _update_task(task_id, status="error", error=error_msg)


def start_platesolve_astap(
    target_name: str,
    exposure_seconds: int = 30,
    iso: str = "400",
    search_radius_deg: float = 1.5,
    focal_length_mm: float = _DEFAULT_FOCAL_LENGTH_MM,
    pixel_size_um: float = _DEFAULT_PIXEL_SIZE_UM,
    hemisphere: str = "north",
) -> str:
    """Start a local plate-solving task in a background thread (capture live).
    Returns the task_id immediately."""
    task_id = create_task()
    thread = threading.Thread(
        target=_run_astap_pipeline,
        args=(task_id, target_name, exposure_seconds, iso, search_radius_deg,
              focal_length_mm, pixel_size_um, hemisphere),
        daemon=True,
    )
    thread.start()
    return task_id


def start_platesolve_from_file_astap(
    file_path: str,
    target_name: str,
    search_radius_deg: float = 1.5,
    focal_length_mm: float = _DEFAULT_FOCAL_LENGTH_MM,
    pixel_size_um: float = _DEFAULT_PIXEL_SIZE_UM,
    hemisphere: str = "north",
) -> str:
    """Start a local plate-solving task from an already-captured file (uploaded).
    Returns the task_id immediately."""
    task_id = create_task()
    thread = threading.Thread(
        target=_run_astap_from_file,
        args=(task_id, file_path, target_name, search_radius_deg,
              focal_length_mm, pixel_size_um, hemisphere),
        daemon=True,
    )
    thread.start()
    return task_id


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(description="astap_cli plate-solve + fully-offline annotation")
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--focal-length", type=float, required=True)
    parser.add_argument("--pixel-size", type=float, required=True)
    parser.add_argument("--ra", required=True)
    parser.add_argument("--dec", required=True)
    args = parser.parse_args()

    wcs_metadata, output_path, dso_list = run_astap_annotation({
        "input_nef": args.input,
        "output_image": args.output,
        "focal_length": args.focal_length,
        "pixel_size": args.pixel_size,
        "ra": args.ra,
        "dec": args.dec,
    })
    if wcs_metadata is None:
        print("[-] Solve/annotation failed -- see log above.")
        sys.exit(1)
    print(f"[*] Solved: ra={wcs_metadata['ra_deg']:.4f} dec={wcs_metadata['dec_deg']:.4f} "
          f"scale={wcs_metadata['scale']:.3f}\"/px rotation={wcs_metadata['rotation']:.2f}")
    print(f"[*] Output: {output_path}")
    print(f"[*] {len(dso_list)} objects annotated")


if __name__ == "__main__":
    main()
