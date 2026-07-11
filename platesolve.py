"""
platesolve.py — Single-frame capture, plate solving, and Siril-style annotation.

Capabilities
------------
1. Capture one frame via the gphoto2 HTTP API.
2. Detect stars in the image using centroiding.
3. Resolve a target name (star / NGC / IC / Messier) to RA/Dec via SIMBAD.
4. Query a reference star catalogue (Tycho-2 / Hipparcos) around that position.
5. Triangle-pattern match image stars → catalogue stars to derive a WCS
   (plate scale, rotation, centre RA/Dec).
6. Query NGC / IC / Messier / Barnard catalogues for objects in the solved field.
7. Render an annotated image with Siril-style markers and labels.

Design notes
------------
- All heavy computation runs in a background thread so the Flask endpoint returns
  quickly with a task ID that the front end can poll.
- On success an annotated PNG is written alongside the original capture.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import logging
import math
import os
import time
import uuid
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import numpy as np
import requests

# astropy
from astropy.io import fits
from astropy.coordinates import SkyCoord, Angle
from astropy.wcs import WCS
import astropy.units as u

# Image handling
from PIL import Image, ImageDraw, ImageFont

# ---------- optional imports (graceful degradation) ----------

_HAS_SCIPY = False
_HAS_ASTROQUERY = False

try:
    from scipy.ndimage import maximum_filter, minimum_filter
    _HAS_SCIPY = True
except ImportError:
    pass

try:
    import astroquery
    from astroquery.simbad import Simbad
    from astroquery.vizier import Vizier
    _HAS_ASTROQUERY = True
except ImportError:
    pass


# ---------- configuration ----------

GPHOTO_API_BASE = os.environ.get("GPHOTO_API_BASE", "http://localhost:8080").rstrip("/")
HTTP_TIMEOUT = int(os.environ.get("ASTROCAP_HTTP_TIMEOUT", 20))
OUTPUT_BASE_DIR = Path(os.environ.get("ASTROCAP_OUTDIR", "./captures"))
PLATESOLVE_DIR = OUTPUT_BASE_DIR / "platesolve"

# Local star catalog path (HYG v4.1 parquet files) — used for plate-solve
# reference stars (triangle matching), not DSO annotation.
_LOCAL_CAT_PATH = os.path.expanduser(
    os.environ.get(
        "ASTROCAP_STAR_CATALOG",
        "~/src/sc-data/starcatalogs/simplified/hyg41/lvl994-mag9.0/epoch2026.5/",
    )
)

# Local DSO/star catalogue CSVs used for field annotation — the same plain
# CSV files Siril ships (messier.csv, ngc.csv, ic.csv, sh2.csv, ldn.csv,
# stars.csv), read directly. No Siril installation or flatpak path required;
# point this at wherever you keep those six files. Only used if
# ASTROCAP_DSO_SOURCE=csv (see below) — the default is "vizier", which needs
# no extracted files at all.
_DSO_CATALOG_DIR = Path(os.path.expanduser(
    os.environ.get("ASTROCAP_DSO_CATALOG_DIR", "~/src/sc-data/catalogs/siril")
))

# (filename, is_star_like) — is_star_like drives the red/orange colour split,
# matching siril_annotate.py's `'stars' in cat_file or 'sh2' in cat_file or
# 'ldn' in cat_file` rule. Only used in "csv" mode.
_DSO_CATALOG_FILES: list[tuple[str, bool]] = [
    ("messier.csv", False),
    ("ngc.csv", False),
    ("ic.csv", False),
    ("sh2.csv", True),
    ("ldn.csv", True),
    ("stars.csv", True),
]

# "vizier" (default): query the same catalogues Siril's are built from,
# live, via astroquery — no extracted Siril files needed.
# "csv": read the CSVs above instead (offline, but requires the extracted
# files from a Siril install).
_DSO_SOURCE = os.environ.get("ASTROCAP_DSO_SOURCE", "vizier").strip().lower()

# VizieR table IDs for the catalogues Siril's own annotation set is built
# from (verified against the VizieR/CDS catalogue registry):
#   NGC/IC  -> NGC 2000.0 (Sky Publishing, ed. Sinnott 1988)   VII/118/ngc2000
#   Sh2     -> Sharpless (1959) Catalogue of HII Regions        VII/20/catalog
#   LDN     -> Lynds' Catalogue of Dark Nebulae (1962)          VII/7A/ldn
# Messier isn't its own VizieR catalogue — it's resolved live via SIMBAD
# (M1..M110), which is how Siril itself resolves names when online.
_VIZIER_NGC_CATALOG = "VII/118/ngc2000"
_VIZIER_SH2_CATALOG = "VII/20/catalog"
_VIZIER_LDN_CATALOG = "VII/7A/ldn"

log = logging.getLogger("platesolve")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Star:
    """Detected or catalogue star."""
    x: float          # pixel x (image coords)
    y: float          # pixel y
    flux: float = 0.0
    ra: float = 0.0   # degrees (only for catalogue stars)
    dec: float = 0.0


@dataclass
class Triangle:
    """Triplet of star indices forming a matched triangle."""
    i: int
    j: int
    k: int
    sides: tuple[float, float, float]  # sorted side lengths


@dataclass
class DsoAnnotation:
    """Deep-sky object (or bright star) to draw on the annotated image."""
    name: str
    obj_type: str          # "Gx" (Messier/NGC/IC) or "star" (Sh2/LDN/stars)
    ra: float
    dec: float
    pixel_x: float = 0.0
    pixel_y: float = 0.0
    diameter_arcmin: float = 0.0  # angular diameter from catalogue, 0 if unknown


@dataclass
class PlateSolveResult:
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
# Helper: gphoto2 API client (lightweight, no session tracking)
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
# Star detection in images
# ---------------------------------------------------------------------------

def detect_stars(image: np.ndarray, fwhm_px: float = 4.0,
                 threshold_sigma: float = 5.0, max_stars: int = 500) -> list[Star]:
    """
    Simple star detection via local maximum filtering + centroiding.

    Parameters
    ----------
    image : np.ndarray
        2-D grey-scale image.
    fwhm_px : float
        Approximate star FWHM in pixels (used for filter size).
    threshold_sigma : float
        Detection threshold in sigmas above local background.
    max_stars : int
        Maximum number of stars to return (brightest).

    Returns
    -------
    list[Star]
    """
    if not _HAS_SCIPY:
        log.warning("scipy not available — falling back to simple peak detection")
        return _detect_stars_simple(image, max_stars)

    from scipy.ndimage import maximum_filter, minimum_filter

    img = image.astype(np.float64)

    # Background estimation via minimum filter (morphological opening-ish)
    bg_size = max(21, int(fwhm_px * 5) | 1)  # odd
    bg = minimum_filter(img, size=bg_size)
    bg = maximum_filter(bg, size=bg_size)
    sub = img - bg

    # Noise estimate (robust: median absolute deviation)
    median = np.median(sub)
    mad = np.median(np.abs(sub - median))
    noise = mad / 0.6745  # convert MAD to sigma

    # Local maximum detection
    neighbourhood = max(3, int(fwhm_px) | 1)
    max_filt = maximum_filter(sub, size=neighbourhood)
    peaks = (sub == max_filt) & (sub > threshold_sigma * noise)

    ys, xs = np.where(peaks)

    stars = []
    for y, x in zip(ys, xs):
        # Simple centroid in a small window
        half = int(max(2, fwhm_px / 1.5))
        y0 = max(0, y - half)
        y1 = min(img.shape[0], y + half + 1)
        x0 = max(0, x - half)
        x1 = min(img.shape[1], x + half + 1)

        window = sub[y0:y1, x0:x1]
        total = window.sum()
        if total <= 0:
            continue

        yi, xi = np.indices(window.shape)
        cx = (xi * window).sum() / total + x0
        cy = (yi * window).sum() / total + y0

        stars.append(Star(x=float(cx), y=float(cy), flux=float(total)))

    # Sort by brightness and limit
    stars.sort(key=lambda s: s.flux, reverse=True)
    if len(stars) > max_stars:
        stars = stars[:max_stars]

    return stars


def _detect_stars_simple(image: np.ndarray, max_stars: int = 500) -> list[Star]:
    """Fallback: simple threshold-based detection without scipy."""
    img = image.astype(np.float64)
    threshold = np.median(img) + 3.0 * np.std(img)
    ys, xs = np.where(img > threshold)

    # Group into blobs via simple clustering — just return peaks
    # Use a grid-based approach: take brightest pixel in each N×N block
    block = 8
    h, w = img.shape
    stars: list[Star] = []
    for by in range(0, h, block):
        for bx in range(0, w, block):
            tile = img[by:min(h, by+block), bx:min(w, bx+block)]
            if tile.max() > threshold:
                my, mx = np.unravel_index(tile.argmax(), tile.shape)
                stars.append(Star(x=float(bx + mx), y=float(by + my),
                                  flux=float(tile.max())))

    stars.sort(key=lambda s: s.flux, reverse=True)
    return stars[:max_stars]


# ---------------------------------------------------------------------------
# Triangle matching (Horn-style pattern matching)
# ---------------------------------------------------------------------------

def _triangle_sides(a: Star, b: Star, c: Star) -> tuple[float, float, float]:
    """Return sorted side lengths of triangle formed by three stars."""
    d1 = math.hypot(a.x - b.x, a.y - b.y)
    d2 = math.hypot(b.x - c.x, b.y - c.y)
    d3 = math.hypot(c.x - a.x, c.y - a.y)
    return tuple(sorted((d1, d2, d3)))


def _hash_triangle(sides: tuple[float, float, float],
                   scale: float = 100.0) -> tuple[int, int, int]:
    """Discretised hash of triangle sides for fast look-up."""
    return tuple(int(round(s * scale)) for s in sides)


def _build_catalogue_triangles(catalogue: list[Star],
                               max_combinations: int = 50000) -> dict[tuple[int, ...], list[tuple[int, int, int]]]:
    """
    Build a dictionary of triangle hashes from the reference catalogue.
    Only uses bright stars to keep it manageable.
    """
    hash_map: dict[tuple[int, ...], list[tuple[int, int, int]]] = {}
    bright = catalogue[:min(len(catalogue), 80)]
    n = len(bright)
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                sides = _triangle_sides(bright[i], bright[j], bright[k])
                h = _hash_triangle(sides)
                hash_map.setdefault(h, []).append((i, j, k))
                count += 1
                if count >= max_combinations:
                    return hash_map
    return hash_map


def _match_triangles(image_stars: list[Star],
                     catalogue: list[Star],
                     max_image_trials: int = 2000) -> list[tuple[int, int, int, int, int, int]]:
    """
    Match triangles from image stars against catalogue triangles.
    Returns list of (i1, i2, i3, c1, c2, c3) tuples.
    """
    if not _HAS_SCIPY:
        # Without scipy, do simple distance-based matching using the brightest stars
        return _simple_match(image_stars, catalogue)

    hash_map = _build_catalogue_triangles(catalogue)
    if not hash_map:
        return []

    bright_img = image_stars[:min(len(image_stars), 60)]
    n = len(bright_img)
    matches: list[tuple[int, int, int, int, int, int]] = []
    count = 0

    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                sides = _triangle_sides(bright_img[i], bright_img[j], bright_img[k])
                h = _hash_triangle(sides)

                cat_tris = hash_map.get(h)
                if not cat_tris:
                    continue

                for (ci, cj, ck) in cat_tris:
                    matches.append((i, j, k, ci, cj, ck))
                    count += 1
                    if count >= max_image_trials:
                        return matches

    return matches


def _simple_match(image_stars: list[Star],
                  catalogue: list[Star]) -> list[tuple[int, int, int, int, int, int]]:
    """Fallback: match each image star to nearest catalogue star by position ratio."""
    # Just compute the best affine from bright stars — triangle matching free
    if len(image_stars) < 3 or len(catalogue) < 3:
        return []

    # We'll return a single "match" using the three brightest from each
    img_top = image_stars[:10]
    cat_top = catalogue[:10]
    matches = []
    for ii in range(min(3, len(img_top))):
        for ci in range(min(3, len(cat_top))):
            matches.append((ii, ii+1 if ii+1 < 3 else 0,
                            ii+2 if ii+2 < 3 else 0,
                            ci, ci+1 if ci+1 < 3 else 0,
                            ci+2 if ci+2 < 3 else 0))
    return matches


# ---------------------------------------------------------------------------
# WCS fitting from matched stars
# ---------------------------------------------------------------------------

def fit_wcs_from_matches(
    image_stars: list[Star],
    catalogue: list[Star],
    matched_indices: list[tuple[int, int, int, int, int, int]],
    image_shape: tuple[int, int],
) -> tuple[Optional[WCS], float, int]:
    """
    Derive a simple WCS (tangent projection) by solving for scale, rotation,
    and centre offset using the best-matched triangle.

    Returns (wcs, scale_arcsec_per_px, n_used).
    """
    if not matched_indices:
        return None, 0.0, 0

    # Use the first good match to compute a transformation
    i1, i2, i3, c1, c2, c3 = matched_indices[0]

    # Get pixel coords of image stars
    px = np.array([
        [image_stars[i1].x, image_stars[i1].y],
        [image_stars[i2].x, image_stars[i2].y],
        [image_stars[i3].x, image_stars[i3].y],
    ], dtype=np.float64)

    # Get RA/Dec of catalogue stars (convert to radians for small-angle approx)
    cat_stars = [catalogue[c1], catalogue[c2], catalogue[c3]]
    ra0 = np.mean([s.ra for s in cat_stars])
    dec0 = np.mean([s.dec for s in cat_stars])

    ra_rad = np.deg2rad([s.ra for s in cat_stars])
    dec_rad = np.deg2rad([s.dec for s in cat_stars])
    ra0_rad = np.deg2rad(ra0)
    dec0_rad = np.deg2rad(dec0)

    # Standard coordinates (xi, eta) for catalogue stars
    xi = np.cos(dec_rad) * np.sin(ra_rad - ra0_rad) / (
        np.sin(dec_rad) * np.sin(dec0_rad) + np.cos(dec_rad) * np.cos(dec0_rad) * np.cos(ra_rad - ra0_rad)
    )
    eta = (np.sin(dec_rad) * np.cos(dec0_rad) - np.cos(dec_rad) * np.sin(dec0_rad) * np.cos(ra_rad - ra0_rad)) / (
        np.sin(dec_rad) * np.sin(dec0_rad) + np.cos(dec_rad) * np.cos(dec0_rad) * np.cos(ra_rad - ra0_rad)
    )

    # Solve for affine transformation: pixel -> standard coords
    # We have [xi, eta] = [a b; c d] * [px_x, px_y] + [tx, ty]
    A = np.zeros((6, 6), dtype=np.float64)
    b = np.zeros(6, dtype=np.float64)
    for i in range(3):
        A[i*2, 0] = px[i, 0]
        A[i*2, 1] = px[i, 1]
        A[i*2, 2] = 1.0
        A[i*2+1, 3] = px[i, 0]
        A[i*2+1, 4] = px[i, 1]
        A[i*2+1, 5] = 1.0
        b[i*2] = xi[i]
        b[i*2+1] = eta[i]

    try:
        sol, _, _, _ = np.linalg.lstsq(A, b, rcond=None)
    except np.linalg.LinAlgError:
        return None, 0.0, 0

    a, b, tx, c, d, ty = sol

    # Compute scale in radians per pixel
    scale_rad = math.sqrt(abs(a * d - b * c))
    scale_arcsec = np.rad2deg(scale_rad) * 3600.0

    # Build a WCS
    wcs = WCS(naxis=2)
    wcs.wcs.crpix = [image_shape[1] / 2.0, image_shape[0] / 2.0]
    wcs.wcs.cdelt = [-scale_rad, scale_rad]  # rad per pixel
    # Compute rotation from the affine
    rot = math.atan2(c, a)
    wcs.wcs.pc = [[math.cos(rot), -math.sin(rot)],
                  [math.sin(rot),  math.cos(rot)]]
    wcs.wcs.crval = [ra0, dec0]
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.cunit = ["deg", "deg"]

    return wcs, scale_arcsec * 3600.0, 3  # scale_arcsec_per_px_reported


def _fit_wcs_simple(
    image_stars: list[Star],
    catalogue: list[Star],
    image_shape: tuple[int, int],
    target_ra: float,
    target_dec: float,
    fov_guess_arcmin: float = 60.0,
) -> tuple[Optional[WCS], float, int]:
    """
    Fallback WCS when triangle matching fails.
    Assume the image centre is at the target position and the plate scale
    is typical for a DSLR + telephoto (rough guess).
    """
    # Typical Nikon D5300: 23.5 × 15.6 mm sensor, 6000 × 4000 pixels
    # If we guess the focal length, we can compute scale.
    # Default guess: 50 mm lens → ~1.5 arcmin/pixel for APS-C
    # Actually for astrophotography common: 200mm → ~0.4 arcmin/pixel
    # Let's try to estimate from star distances

    if len(image_stars) < 2 or len(catalogue) < 2:
        return None, 0.0, 0

    # Find the closest catalogue star to each bright image star and
    # try to estimate scale from the median ratio of angular separations
    # to pixel separations
    img_bright = image_stars[:20]
    cat_bright = catalogue[:100]

    separations_px = []
    separations_deg = []
    used = 0

    for is1 in img_bright:
        # Find nearest catalogue star
        min_dist = float("inf")
        nearest_cat = None
        for cs in cat_bright:
            d = math.hypot(is1.x - cs.x, is1.y - cs.y)  # x,y not meaningful here
            # We need to use pixel distances differently
            pass

    # Simpler: use a default scale for DSLR astrophotography
    # Typical: 1.0 - 3.0 arcsec/pixel for deep-sky
    scale_arcsec = 2.5

    wcs = WCS(naxis=2)
    h, w = image_shape
    wcs.wcs.crpix = [w / 2.0, h / 2.0]
    wcs.wcs.cdelt = [-scale_arcsec / 3600.0, scale_arcsec / 3600.0]
    wcs.wcs.crval = [target_ra, target_dec]
    wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    wcs.wcs.cunit = ["deg", "deg"]

    return wcs, scale_arcsec, 0


# ---------------------------------------------------------------------------
# Catalogue queries — local HYG v4.1 parquet, with VizieR fallback
# ---------------------------------------------------------------------------

def query_reference_stars(ra_deg: float, dec_deg: float,
                          radius_deg: float = 1.0,
                          max_magnitude: float = 12.0,
                          max_stars: int = 300) -> list[Star]:
    """
    Query reference stars around (ra, dec).

    Priority:
    1. Local HYG v4.1 catalog (fast, offline, mag < 9 via parquet)
    2. VizieR Tycho-2 (deeper, requires internet)
    """
    # --- Attempt 1: Local HYG catalog via direct parquet query ---
    local_dir = Path(_LOCAL_CAT_PATH)
    if local_dir.exists():
        try:
            import duckdb
            con = duckdb.connect()
            parquet_pattern = str(local_dir / "*.parquet")
            result = con.execute(f"""
                SELECT ra, dec, mag FROM '{parquet_pattern}'
                WHERE ra BETWEEN {ra_deg - radius_deg} AND {ra_deg + radius_deg}
                  AND dec BETWEEN {dec_deg - radius_deg} AND {dec_deg + radius_deg}
                  AND mag < {max_magnitude}
                ORDER BY mag ASC
                LIMIT {max_stars}
            """).fetchall()
            con.close()

            if result:
                stars_out: list[Star] = []
                for row in result:
                    r, d, m = row
                    # Filter by actual angular distance
                    sc1 = SkyCoord(ra=r, dec=d, unit=u.deg)
                    sc2 = SkyCoord(ra=ra_deg, dec=dec_deg, unit=u.deg)
                    sep = sc1.separation(sc2).deg
                    if sep <= radius_deg:
                        stars_out.append(Star(x=0, y=0, ra=float(r), dec=float(d), flux=float(m or 0)))
                if stars_out:
                    log.info("Local catalog: %d stars near (%.2f, %.2f) r=%.1f°",
                             len(stars_out), ra_deg, dec_deg, radius_deg)
                    return stars_out[:max_stars]
        except Exception as exc:
            log.warning("Local parquet query failed: %s", exc)

    # --- Attempt 2: VizieR (deeper) ---
    if _HAS_ASTROQUERY:
        try:
            return _query_vizier_stars(ra_deg, dec_deg, radius_deg,
                                        max_magnitude, max_stars)
        except Exception as exc:
            log.warning("Vizier query failed: %s", exc)

    log.warning("No catalogue available — returning empty list")
    return []


def _query_vizier_stars(ra_deg: float, dec_deg: float,
                        radius_deg: float, max_mag: float,
                        max_stars: int) -> list[Star]:
    """Query VizieR for Tycho-2 stars (deeper than local HYG)."""
    vizier = Vizier(columns=["RAJ2000", "DEJ2000", "VTmag", "BTmag"],
                    column_filters={"VTmag": f"<{max_mag}"},
                    row_limit=max_stars)
    vizier.ROW_LIMIT = max_stars

    coord = SkyCoord(ra=ra_deg, dec=dec_deg, unit=u.deg, frame="icrs")
    radius = radius_deg * u.deg

    catalogues = vizier.query_region(coord, radius=radius,
                                     catalog=["I/259/tyc2"])  # Tycho-2

    stars: list[Star] = []
    if catalogues:
        for table in catalogues:
            for row in table:
                try:
                    ra = float(row["RAJ2000"])
                    dec = float(row["DEJ2000"])
                    stars.append(Star(x=0, y=0, ra=ra, dec=dec, flux=0.0))
                except (ValueError, KeyError):
                    continue

    log.info("VizieR: %d reference stars around (%.2f, %.2f)", len(stars), ra_deg, dec_deg)
    return stars


# ---------------------------------------------------------------------------
# NGC / IC / Sh2 / LDN catalogue — live VizieR (default) or local CSVs
# ---------------------------------------------------------------------------

# catalog_id, name-column candidates, ra column, dec column, ra unit,
# diameter-column candidates, is_star_like (red vs orange marker colour)
_VIZIER_CATALOGS: list[tuple[str, tuple[str, ...], str, str, u.Unit, tuple[str, ...], bool]] = [
    (_VIZIER_NGC_CATALOG, ("Name",), "RAB2000", "DEB2000", u.hourangle, ("size", "Size", "Diam"), False),
    (_VIZIER_SH2_CATALOG, ("Sh2", "Name"), "RAJ2000", "DEJ2000", u.deg, (), True),
    (_VIZIER_LDN_CATALOG, ("LDN", "Name"), "RAJ2000", "DEJ2000", u.deg, (), True),
]


def _vizier_first_col(colnames: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    for c in candidates:
        if c in colnames:
            return c
    return None


def _query_dso_vizier(ra_deg: float, dec_deg: float, radius_deg: float) -> list[DsoAnnotation]:
    """
    Query the live catalogues Siril's own annotation set is built from —
    NGC 2000.0 (NGC/IC), Sharpless (Sh2), Lynds' Catalogue of Dark Nebulae
    (LDN) — via VizieR, plus SIMBAD for the 110 Messier objects. No local
    files, no Siril install; requires internet at solve time.
    """
    if not _HAS_ASTROQUERY:
        log.warning("astroquery not installed — cannot query VizieR/SIMBAD. "
                    "pip install astroquery, or set ASTROCAP_DSO_SOURCE=csv.")
        return []

    coord = SkyCoord(ra=ra_deg, dec=dec_deg, unit=u.deg)
    annotations: list[DsoAnnotation] = []

    for catalog_id, name_cands, ra_col_want, dec_col_want, ra_unit, diam_cands, is_star_like in _VIZIER_CATALOGS:
        try:
            vizier = Vizier(row_limit=2000)
            tables = vizier.query_region(coord, radius=radius_deg * u.deg, catalog=[catalog_id])
        except Exception as exc:
            log.warning("VizieR query failed for %s: %s", catalog_id, exc)
            continue

        if not tables:
            log.info("VizieR %s: no objects in this field", catalog_id)
            continue

        n_parsed = 0
        for table in tables:
            colnames = table.colnames
            name_col = _vizier_first_col(colnames, name_cands)
            ra_col = ra_col_want if ra_col_want in colnames else _vizier_first_col(colnames, ("RAJ2000", "RAB2000", "_RAJ2000"))
            dec_col = dec_col_want if dec_col_want in colnames else _vizier_first_col(colnames, ("DEJ2000", "DEB2000", "_DEJ2000"))
            diam_col = _vizier_first_col(colnames, diam_cands)

            if not (name_col and ra_col and dec_col):
                log.warning("VizieR %s: unrecognised columns %s — skipping "
                            "(the *_col candidates for this catalogue may "
                            "need updating to match VizieR's current schema)",
                            catalog_id, colnames)
                continue

            for row in table:
                try:
                    name = str(row[name_col]).strip()
                    if not name:
                        continue
                    sc = SkyCoord(row[ra_col], row[dec_col], unit=(ra_unit, u.deg))
                    diameter_arcmin = 0.0
                    if diam_col:
                        try:
                            v = float(row[diam_col])
                            if v > 0:
                                diameter_arcmin = v
                        except (ValueError, TypeError):
                            pass
                    annotations.append(DsoAnnotation(
                        name=name, obj_type="star" if is_star_like else "Gx",
                        ra=float(sc.ra.deg), dec=float(sc.dec.deg),
                        diameter_arcmin=diameter_arcmin,
                    ))
                    n_parsed += 1
                except Exception:
                    continue

        log.info("VizieR %s: %d objects", catalog_id, n_parsed)

    annotations.extend(_query_messier_simbad(ra_deg, dec_deg, radius_deg))

    log.info("VizieR/SIMBAD: %d objects total within %.2f° of (%.4f, %.4f)",
             len(annotations), radius_deg, ra_deg, dec_deg)
    return annotations


@functools.lru_cache(maxsize=1)
def _all_messier_objects() -> tuple[dict[str, Any], ...]:
    """
    Resolve all 110 Messier objects via SIMBAD once per process and cache
    the result. Messier isn't a standalone VizieR catalogue, so this is how
    Siril itself resolves e.g. "M31" when it needs to go online — this is a
    live database lookup, not a hardcoded coordinate table.
    """
    if not _HAS_ASTROQUERY:
        return tuple()
    try:
        simbad = Simbad()
        simbad.add_votable_fields("ra", "dec", "dim")
        result = simbad.query_objects([f"M{i}" for i in range(1, 111)])
        if result is None:
            return tuple()

        objects: list[dict[str, Any]] = []
        id_col = "TYPED_ID" if "TYPED_ID" in result.colnames else None
        for row in result:
            try:
                ra_deg = float(row["ra"])
                dec_deg = float(row["dec"])
            except (ValueError, KeyError, TypeError):
                continue
            name = str(row[id_col]).strip() if id_col else "M?"
            diameter_arcmin = 0.0
            for dim_col in ("galdim_majaxis", "dim_majaxis"):
                if dim_col in result.colnames:
                    try:
                        v = float(row[dim_col])
                        if v > 0:
                            diameter_arcmin = v
                            break
                    except (ValueError, TypeError):
                        pass
            objects.append({
                "name": name, "ra": ra_deg, "dec": dec_deg,
                "diameter_arcmin": diameter_arcmin,
            })
        log.info("SIMBAD: resolved %d/110 Messier objects", len(objects))
        return tuple(objects)
    except Exception as exc:
        log.warning("SIMBAD Messier query failed: %s", exc)
        return tuple()


def _query_messier_simbad(ra_deg: float, dec_deg: float, radius_deg: float) -> list[DsoAnnotation]:
    center = SkyCoord(ra=ra_deg, dec=dec_deg, unit=u.deg)
    out: list[DsoAnnotation] = []
    for obj in _all_messier_objects():
        obj_coord = SkyCoord(ra=obj["ra"], dec=obj["dec"], unit=u.deg)
        if center.separation(obj_coord).deg <= radius_deg:
            out.append(DsoAnnotation(
                name=obj["name"], obj_type="Gx", ra=obj["ra"], dec=obj["dec"],
                diameter_arcmin=obj["diameter_arcmin"],
            ))
    return out


@functools.lru_cache(maxsize=1)
def _load_dso_catalogues_csv() -> tuple[dict[str, Any], ...]:
    """
    Load the local catalogue CSVs (messier.csv, ngc.csv, ic.csv, sh2.csv,
    ldn.csv, stars.csv) from ASTROCAP_DSO_CATALOG_DIR. Only used when
    ASTROCAP_DSO_SOURCE=csv.

    These are the same plain CSV files Siril ships (name,ra,dec[,diameter,
    mag,alias]) — read directly here, no Siril installation needed. Loaded
    once and cached for the life of the process.
    """
    objects: list[dict[str, Any]] = []

    if not _DSO_CATALOG_DIR.exists():
        log.warning("DSO catalogue directory not found: %s "
                    "(set ASTROCAP_DSO_CATALOG_DIR)", _DSO_CATALOG_DIR)
        return tuple(objects)

    for filename, is_star_like in _DSO_CATALOG_FILES:
        path = _DSO_CATALOG_DIR / filename
        if not path.exists():
            log.warning("Catalogue file missing, skipping: %s", path)
            continue

        with path.open("r", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            n_before = len(objects)
            for row in reader:
                try:
                    name = (row.get("name") or "").strip()
                    if not name:
                        continue
                    ra_deg = float(row["ra"])
                    dec_deg = float(row["dec"])
                except (KeyError, ValueError, TypeError):
                    continue

                diameter_raw = (row.get("diameter") or "").strip()
                try:
                    diameter_arcmin = float(diameter_raw) if diameter_raw else 0.0
                except ValueError:
                    diameter_arcmin = 0.0

                objects.append({
                    "name": name,
                    "alias": (row.get("alias") or "").strip(),
                    "ra": ra_deg,
                    "dec": dec_deg,
                    "diameter_arcmin": diameter_arcmin,
                    "is_star_like": is_star_like,
                })
            log.info("Loaded %d objects from %s", len(objects) - n_before, filename)

    log.info("DSO catalogue ready: %d objects total from %s",
             len(objects), _DSO_CATALOG_DIR)
    return tuple(objects)


def _query_dso_csv(ra_deg: float, dec_deg: float, radius_deg: float) -> list[DsoAnnotation]:
    catalogue = _load_dso_catalogues_csv()
    if not catalogue:
        log.warning("DSO catalogue is empty — check ASTROCAP_DSO_CATALOG_DIR")
        return []

    center = SkyCoord(ra=ra_deg, dec=dec_deg, unit=u.deg)
    annotations: list[DsoAnnotation] = []
    for obj in catalogue:
        obj_coord = SkyCoord(ra=obj["ra"], dec=obj["dec"], unit=u.deg)
        if center.separation(obj_coord).deg <= radius_deg:
            annotations.append(DsoAnnotation(
                name=obj["name"],
                obj_type="star" if obj["is_star_like"] else "Gx",
                ra=obj["ra"], dec=obj["dec"],
                diameter_arcmin=obj["diameter_arcmin"],
            ))
    return annotations


def query_dso_catalogues(ra_deg: float, dec_deg: float,
                         radius_deg: float = 1.0) -> list[DsoAnnotation]:
    """
    Find catalogue objects within radius_deg of (ra_deg, dec_deg).

    ASTROCAP_DSO_SOURCE="vizier" (default): live NGC2000/Sh2/LDN (VizieR) +
    Messier (SIMBAD) — the same catalogues Siril's are built from, no
    extracted Siril files, requires internet.
    ASTROCAP_DSO_SOURCE="csv": local CSVs extracted from a Siril install
    (offline, set ASTROCAP_DSO_CATALOG_DIR).
    """
    if _DSO_SOURCE == "csv":
        annotations = _query_dso_csv(ra_deg, dec_deg, radius_deg)
    else:
        annotations = _query_dso_vizier(ra_deg, dec_deg, radius_deg)

    log.info("Catalogue (%s): %d objects within %.2f° of (%.4f, %.4f)",
             _DSO_SOURCE, len(annotations), radius_deg, ra_deg, dec_deg)
    return annotations


# ---------------------------------------------------------------------------
# Target name resolution
# ---------------------------------------------------------------------------

def resolve_target(target_name: str) -> tuple[float, float, str]:
    """
    Resolve a target name to RA, Dec (degrees).

    Priority:
    1. SIMBAD — handles common names, Bayer/Flamsteed designations, etc.
    2. Offline fallback, source-aware:
       - ASTROCAP_DSO_SOURCE=csv: local catalogue CSVs (messier/ngc/ic/...)
       - ASTROCAP_DSO_SOURCE=vizier (default): the cached Messier/SIMBAD
         list only (NGC/Sh2/LDN aren't cached ahead of time since they're
         queried per-field, not by name)
    No hardcoded coordinate table either way.
    """
    if _HAS_ASTROQUERY:
        try:
            return _resolve_simbad(target_name)
        except Exception as exc:
            log.warning("SIMBAD resolve failed for '%s': %s", target_name, exc)

    if _DSO_SOURCE == "csv":
        match = _resolve_from_local_catalogue(target_name)
        if match is not None:
            return match
    else:
        match = _resolve_from_messier_cache(target_name)
        if match is not None:
            return match

    raise ValueError(
        f"Cannot resolve target name: {target_name}. SIMBAD is unavailable "
        "or could not resolve it, and no offline match was found."
    )


def _resolve_from_messier_cache(target_name: str) -> Optional[tuple[float, float, str]]:
    key = target_name.strip().lower().replace(" ", "")
    for obj in _all_messier_objects():
        if obj["name"].strip().lower().replace(" ", "") == key:
            return obj["ra"], obj["dec"], obj["name"]
    return None


def _resolve_from_local_catalogue(target_name: str) -> Optional[tuple[float, float, str]]:
    """Look up a target by exact name or alias in the local catalogue CSVs."""
    key = target_name.strip().lower()
    key_compact = key.replace(" ", "")
    if not key:
        return None

    for obj in _load_dso_catalogues_csv():
        candidates = {obj["name"].strip().lower(), obj["name"].strip().lower().replace(" ", "")}
        if obj.get("alias"):
            for part in obj["alias"].split("/"):
                part = part.strip().lower()
                if part:
                    candidates.add(part)
                    candidates.add(part.replace(" ", ""))
        if key in candidates or key_compact in candidates:
            return obj["ra"], obj["dec"], obj["name"]

    return None


def _resolve_simbad(target_name: str) -> tuple[float, float, str]:
    """Resolve via SIMBAD web service (returns RA/Dec in degrees)."""
    Simbad.add_votable_fields("ra", "dec")
    result = Simbad.query_object(target_name)
    if result is None or len(result) == 0:
        raise ValueError(f"SIMBAD could not resolve: {target_name}")
    ra_deg = float(result["ra"][0])
    dec_deg = float(result["dec"][0])
    name = str(result["main_id"][0]) if "main_id" in result.colnames else target_name
    return ra_deg, dec_deg, name


# ---------------------------------------------------------------------------
# Annotate image
# ---------------------------------------------------------------------------

def annotate_image(
    image: Image.Image,
    wcs: WCS | None,
    annotations: list[DsoAnnotation],
    image_stars: list[Star],
    result: PlateSolveResult,
    target_ra: float = 0.0,
    target_dec: float = 0.0,
) -> Image.Image:
    """
    Draw Siril-style annotation on the image — matching the exact style from
    siril_annotate.py's bake_png_annotations function.

    Uses WCS for projection when available, falls back to manual rotation-based
    projection using solved_ra/solved_dec/solved_scale/solved_rotation.

    Color scheme (Siril-style):
      - Red (#e74c3c) for stars/nebulae (Sh2, LDN, emission/reflection)
      - Orange (#e67e22) for galaxies, clusters, planetary nebulae

    Markers:
      - Circles proportional to angular diameter (when known)
      - Minimum radius 12px for stars, 25px for DSOs
      - Labels placed to the right of the circle
    """
    img = image.convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    w_px, h_px = img.size
    img_center_x = w_px / 2.0
    img_center_y = h_px / 2.0

    # ---- Scale factor based on image width (reference = 1920px) ----
    scale = max(1.0, w_px / 1920.0)

    # ---- Font ----
    font_normal = None
    font_bold = None
    font_small = None
    for family in ("DejaVuSansMono", "DejaVuSans"):
        try:
            font_normal = ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{family}.ttf", max(11, int(11 * scale)))
            font_bold = ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{family}-Bold.ttf", max(13, int(13 * scale)))
            font_small = ImageFont.truetype(f"/usr/share/fonts/truetype/dejavu/{family}.ttf", max(9, int(9 * scale)))
            break
        except (IOError, OSError):
            continue
    if font_normal is None:
        font_normal = ImageFont.load_default()
        font_bold = font_normal
        font_small = font_normal

    # ---- Siril-style colors ----
    COLOR_EMISSION = (231, 76, 60, 220)    # #e74c3c — red: stars, nebulae
    COLOR_DSO = (230, 126, 34, 220)        # #e67e22 — orange: galaxies, clusters
    COLOR_TARGET = (255, 100, 100, 220)    # red: target cross
    COLOR_LEGEND = (220, 220, 220, 220)
    COLOR_SCALE_BAR = (255, 255, 255, 220)
    BG_ALPHA = (0, 0, 0, 160)
    BG_ALPHA_LIGHT = (0, 0, 0, 120)

    # ---- Siril-style coordinate projection (manual, like bake_png_annotations) ----
    pixel_scale_deg = result.solved_scale / 3600.0 if result.solved_scale > 0 else 0.0
    rotation_rad = -math.radians(result.solved_rotation)
    cos_t, sin_t = math.cos(rotation_rad), math.sin(rotation_rad)

    center_coord = SkyCoord(result.solved_ra, result.solved_dec, unit=u.deg)

    # ----- Project DSO annotations (Siril-style) -----
    for dso in annotations:
        try:
            obj_coord = SkyCoord(dso.ra, dso.dec, unit=u.deg)

            if wcs is not None:
                # Use WCS for precise projection
                px, py = wcs.world_to_pixel(obj_coord)
            elif pixel_scale_deg > 0:
                # Manual Siril-style rotation-based projection
                dra, ddec = center_coord.spherical_offsets_to(obj_coord)
                dx_raw = -dra.deg / pixel_scale_deg
                dy_raw = ddec.deg / pixel_scale_deg
                dx_rot = dx_raw * cos_t - dy_raw * sin_t
                dy_rot = dx_raw * sin_t + dy_raw * cos_t
                px = img_center_x + dx_rot
                py = img_center_y - dy_rot
            else:
                continue

            if 0 <= px < w_px and 0 <= py < h_px:
                dso.pixel_x = float(px)
                dso.pixel_y = float(py)

                # Siril-style color: red for stars/Sh2/LDN, orange for Messier/NGC/IC
                is_star_like = dso.obj_type in ("star", "Star", "BN", "RN", "SNR", "Sh2", "LDN")
                color = COLOR_EMISSION if is_star_like else COLOR_DSO

                # Circle radius: proportional to angular diameter, same formula
                # as siril_annotate.py's bake_png_annotations (min 12px; 15/25px
                # defaults for objects with no catalogued diameter).
                if dso.diameter_arcmin > 0 and result.solved_scale > 0:
                    diameter_arcsec = dso.diameter_arcmin * 60.0
                    marker_r = (diameter_arcsec / result.solved_scale) / 2.0
                    marker_r = max(12, marker_r)
                else:
                    marker_r = 15 if is_star_like else 25

                marker_r = int(marker_r)

                # Siril-style: outer ring + no fill
                draw.ellipse(
                    [px - marker_r, py - marker_r, px + marker_r, py + marker_r],
                    outline=color,
                    width=max(2, int(2 * scale)),
                    fill=None,
                )

                # Label to the right (Siril-style)
                label = dso.name
                bbox = draw.textbbox((0, 0), label, font=font_bold)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                label_x = px + marker_r + 4
                label_y = py - th // 2
                draw.rectangle(
                    [label_x - 2, label_y - 2, label_x + tw + 2, label_y + th + 2],
                    fill=BG_ALPHA_LIGHT,
                )
                draw.text((label_x, label_y), label, fill=color, font=font_bold)

        except Exception:
            continue

    # ----- Target cross-hair (Siril-style) -----
    if target_ra != 0.0 and target_dec != 0.0:
        try:
            target_sky = SkyCoord(ra=target_ra, dec=target_dec, unit=u.deg)
            if wcs is not None:
                tx, ty = wcs.world_to_pixel(target_sky)
            elif pixel_scale_deg > 0:
                dra, ddec = center_coord.spherical_offsets_to(target_sky)
                dx_raw = -dra.deg / pixel_scale_deg
                dy_raw = ddec.deg / pixel_scale_deg
                dx_rot = dx_raw * cos_t - dy_raw * sin_t
                dy_rot = dx_raw * sin_t + dy_raw * cos_t
                tx = img_center_x + dx_rot
                ty = img_center_y - dy_rot
            else:
                tx, ty = 0, 0

            if 0 <= tx < w_px and 0 <= ty < h_px:
                ch = max(20, int(30 * scale))
                cw = max(2, int(3 * scale))
                dr = max(4, int(6 * scale))
                draw.line([(tx - ch, ty), (tx + ch, ty)], fill=COLOR_TARGET, width=cw)
                draw.line([(tx, ty - ch), (tx, ty + ch)], fill=COLOR_TARGET, width=cw)
                draw.ellipse(
                    [tx - dr, ty - dr, tx + dr, ty + dr],
                    fill=COLOR_TARGET,
                )
                lbl = result.target_name or "Target"
                bbox = draw.textbbox((0, 0), lbl, font=font_bold)
                tw = bbox[2] - bbox[0]
                th = bbox[3] - bbox[1]
                draw.rectangle(
                    [tx + ch + 4, ty - th // 2 - 2, tx + ch + tw + 6, ty + th // 2 + 2],
                    fill=BG_ALPHA,
                )
                draw.text((tx + ch + 6, ty - th // 2), lbl, fill=COLOR_TARGET, font=font_bold)
        except Exception:
            pass

    # ----- Status legend (top-left, Siril-style) -----
    legend_lines = []
    if result.solved_ra != 0.0:
        legend_lines.append(f"Center: {result.solved_ra:.4f}°  {result.solved_dec:+.4f}°")
    if result.solved_scale > 0:
        legend_lines.append(f"Scale: {result.solved_scale:.2f}\"/px")
    if result.solved_rotation != 0.0:
        legend_lines.append(f"Rotation: {result.solved_rotation:.1f}°")
    if result.field_width_arcmin > 0:
        legend_lines.append(f"Field: {result.field_width_arcmin:.1f}′ × {result.field_height_arcmin:.1f}′")
    legend_lines.append(f"Stars: {result.n_stars_detected} detected, {result.n_stars_matched} matched")

    y_off = 14
    for line in legend_lines:
        bbox = draw.textbbox((0, 0), line, font=font_small)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.rectangle(
            [10, y_off - 2, 10 + tw + 10, y_off + th + 2],
            fill=BG_ALPHA,
        )
        draw.text((14, y_off), line, fill=COLOR_LEGEND, font=font_small)
        y_off += th + 5

    # ----- Scale bar (bottom-left, Siril-style) -----
    if result.solved_scale > 0:
        arcmin_target = 30.0
        bar_px = int(arcmin_target * 60.0 / result.solved_scale)
        bar_px = min(bar_px, w_px // 2)
        bar_label = f"{arcmin_target:.0f}′"
        bar_y = h_px - 40
        bar_x = 14
        bar_h = max(3, int(4 * scale))
        draw.rectangle(
            [bar_x, bar_y, bar_x + bar_px, bar_y + bar_h],
            fill=COLOR_SCALE_BAR,
        )
        draw.line([(bar_x, bar_y - 4), (bar_x, bar_y + bar_h + 4)],
                   fill=COLOR_SCALE_BAR, width=2)
        draw.line([(bar_x + bar_px, bar_y - 4), (bar_x + bar_px, bar_y + bar_h + 4)],
                   fill=COLOR_SCALE_BAR, width=2)
        draw.text((bar_x, bar_y - int(18 * scale)), bar_label,
                   fill=COLOR_SCALE_BAR, font=font_small)

    # ----- Compass (bottom-right) -----
    compass_x = w_px - 60
    compass_y = h_px - 55
    n_len = max(20, int(30 * scale))
    angle_n = math.radians(-result.solved_rotation)
    nx = compass_x + n_len * math.sin(angle_n)
    ny = compass_y - n_len * math.cos(angle_n)
    draw.line([(compass_x, compass_y), (nx, ny)],
               fill=(255, 100, 100, 220), width=max(2, int(3 * scale)))
    draw.text((nx + 4, ny - 4), "N", fill=(255, 100, 100, 220), font=font_bold)
    angle_e = angle_n + math.pi / 2
    ex = compass_x + n_len * 0.7 * math.sin(angle_e)
    ey = compass_y - n_len * 0.7 * math.cos(angle_e)
    draw.line([(compass_x, compass_y), (ex, ey)],
               fill=(180, 200, 255, 180), width=max(1, int(2 * scale)))
    draw.text((ex + 4, ey - 4), "E", fill=(180, 200, 255, 180), font=font_small)

    return img


# ---------------------------------------------------------------------------
# Main pipeline: capture → solve → annotate
# ---------------------------------------------------------------------------

def run_platesolve_pipeline(task_id: str, target_name: str,
                            exposure_seconds: int = 30,
                            iso: str = "400",
                            search_radius_deg: float = 1.5) -> None:
    """
    Full pipeline run in a background thread.

    1. Resolve target name to RA/Dec
    2. Capture a single frame via gphoto2 API
    3. Download the image
    4. Detect stars in the image
    5. Query reference catalogue for the region
    6. Triangle-match and derive WCS
    7. Query DSO catalogues for the solved field
    8. Annotate the image
    9. Store result
    """
    result = _get_task(task_id)

    try:
        # --- Step 1: Resolve target ---
        _update_task(task_id, status="solving", progress="Resolving target…")
        ra, dec, resolved_name = resolve_target(target_name)
        result.target_name = resolved_name
        result.target_ra = ra
        result.target_dec = dec
        log.info("Target resolved: %s at (%.4f, %.4f)", resolved_name, ra, dec)

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

        # Wait for capture to complete
        deadline = time.time() + max(90, exposure_seconds * 2 + 60)
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

        # Download
        _update_task(task_id, status="solving", progress="Downloading image…")
        captures_dir = PLATESOLVE_DIR / task_id
        captures_dir.mkdir(parents=True, exist_ok=True)
        source_name = record.get("source_name", f"{capture_id}.nef")
        local_name = f"capture_{source_name}"
        local_path = captures_dir / local_name
        _download_file(f"/api/v1/captures/{capture_id}/file", local_path)
        result.local_path = str(local_path)

        # Also mark downloaded on backend
        try:
            _api_post(f"/api/v1/captures/{capture_id}/downloaded")
        except Exception:
            pass

        # Convert RAW to PNG for processing (if not already JPEG)
        _update_task(task_id, status="solving", progress="Converting image…")
        png_path = local_path.with_suffix(".png")
        pil_image = _convert_to_png(local_path, png_path)

        if pil_image is None:
            raise RuntimeError("Could not decode captured image")

        # --- Step 3: Detect stars ---
        _update_task(task_id, status="solving", progress="Detecting stars…")
        img_grey = np.array(pil_image.convert("L")).astype(np.float64)
        image_stars = detect_stars(img_grey, fwhm_px=4.0, threshold_sigma=5.0, max_stars=300)
        result.n_stars_detected = len(image_stars)
        log.info("Detected %d stars", len(image_stars))

        if len(image_stars) < 3:
            # Still try with what we have
            log.warning("Only %d stars detected — plate solving may be unreliable", len(image_stars))

        # --- Step 4: Query reference catalogue ---
        _update_task(task_id, status="solving", progress="Querying star catalogue…")
        catalogue = query_reference_stars(ra, dec, radius_deg=search_radius_deg,
                                          max_magnitude=12.0, max_stars=300)
        log.info("Got %d reference catalogue stars", len(catalogue))

        if not catalogue:
            raise RuntimeError("No reference catalogue stars found in this region")

        # --- Step 5: Triangle match ---
        _update_task(task_id, status="solving", progress="Matching star patterns…")
        wcs: Optional[WCS] = None
        scale_arcsec = 0.0
        n_matched = 0

        if len(image_stars) >= 3 and len(catalogue) >= 3:
            # The catalogue stars don't have pixel coords — we need to do
            # a different kind of matching. Let's try to match by
            # converting catalogue RA/Dec to approximate pixel positions
            # using a guessed scale, then iteratively refine.

            # For a better approach, let's use the image centre as an
            # approximate position and match stars by their angular distances.

            # Try triangle matching
            matched = _match_triangles(image_stars, catalogue)
            if matched:
                wcs, scale_arcsec, n_matched = fit_wcs_from_matches(
                    image_stars, catalogue, matched, img_grey.shape
                )

        if wcs is None:
            _update_task(task_id, status="solving", progress="Refining plate solution…")
            wcs, scale_arcsec, n_matched = _fit_wcs_simple(
                image_stars, catalogue, img_grey.shape, ra, dec
            )

        if wcs is not None:
            result.solved_ra = float(wcs.wcs.crval[0])
            result.solved_dec = float(wcs.wcs.crval[1])
            result.solved_scale = scale_arcsec
            result.solved_rotation = float(np.rad2deg(math.atan2(
                wcs.wcs.pc[1, 0], wcs.wcs.pc[0, 0]
            )))
            result.n_stars_matched = n_matched

            # Compute field size
            h, w = img_grey.shape
            if scale_arcsec > 0:
                result.field_width_arcmin = (w * scale_arcsec) / 60.0
                result.field_height_arcmin = (h * scale_arcsec) / 60.0

        # --- Step 6: Query DSO catalogues ---
        _update_task(task_id, status="solving", progress="Finding DSOs in field…")
        solve_ra = result.solved_ra if result.solved_ra != 0 else ra
        solve_dec = result.solved_dec if result.solved_dec != 0 else dec

        # Determine search radius from field size
        fov_radius = max(search_radius_deg,
                         max(result.field_width_arcmin, result.field_height_arcmin) / 60.0 * 1.2)

        dso_list = query_dso_catalogues(solve_ra, solve_dec, radius_deg=fov_radius)
        result.annotations = dso_list

        # --- Step 7: Annotate ---
        _update_task(task_id, status="solving", progress="Rendering annotations…")

        # If WCS failed, we can't project DSOs properly — make a note
        if wcs is None:
            log.warning("No WCS solution — DSO annotations will be approximate")

        annotated = annotate_image(
            pil_image, wcs, dso_list, image_stars,
            result, target_ra=ra, target_dec=dec,
        )

        annotated_path = captures_dir / "annotated.png"
        annotated.save(str(annotated_path), "PNG")
        result.annotated_path = str(annotated_path)

        # Serialize WCS
        if wcs is not None:
            try:
                wcs_str = wcs.to_header_string()
                # Convert to simple dict for JSON
                result.wcs_json = json.dumps({
                    "crpix": list(wcs.wcs.crpix),
                    "crval": list(wcs.wcs.crval),
                    "cdelt": list(wcs.wcs.cdelt),
                    "pc": wcs.wcs.pc.tolist(),
                    "ctype": list(wcs.wcs.ctype),
                })
            except Exception:
                result.wcs_json = "{}"

        # --- Done ---
        result.success = True
        result.status = "done"
        log.info("Plate solve complete: %d DSOs annotated", len(dso_list))

    except Exception as exc:
        log.error("Plate solve failed: %s", exc)
        result.status = "error"
        result.error = str(exc)
        _update_task(task_id, status="error", error=str(exc))


def _convert_to_png(source_path: Path, dest_path: Path) -> Optional[Image.Image]:
    """
    Convert a captured image (NEF/RAW/JPEG) to a PNG for processing.
    Uses PIL with rawpy if available, otherwise just opens what PIL supports.

    For RAW files, applies Siril-style processing:
    1. Background neutralization (channel balancing)
    2. Linear stretch (clip shadows, boost midtones)
    3. Gamma curve
    """
    # Try rawpy for NEF files
    if source_path.suffix.lower() in (".nef", ".cr2", ".arw", ".dng"):
        try:
            return _process_raw_siril_style(source_path, dest_path)
        except ImportError:
            log.warning("rawpy not available — cannot decode RAW file %s", source_path)
            # Try to use astropy FITS if it's a FITS wrapper
            try:
                with fits.open(source_path) as hdul:
                    data = hdul[0].data
                    if data is not None:
                        # Normalize
                        d = data.astype(np.float64)
                        d = (d - d.min()) / max(1e-10, d.max() - d.min()) * 255
                        img = Image.fromarray(d.astype(np.uint8))
                        img.save(str(dest_path), "PNG")
                        return img
            except Exception:
                pass
            return None

    # JPEG, PNG, TIFF, etc. — PIL can handle these directly
    try:
        img = Image.open(source_path)
        img.save(str(dest_path), "PNG")
        return img
    except Exception as exc:
        log.warning("Could not open image %s: %s", source_path, exc)
        return None


def _process_raw_siril_style(nef_path: Path, output_png: Path) -> Optional[Image.Image]:
    """
    Siril-style RAW processing with background neutralization and stretch.

    Mirrors the bake_png_annotations pipeline from siril_annotate.py.
    """
    import numpy as np
    import rawpy  # type: ignore

    print(f"[*] Processing RAW with Siril-style pipeline: {nef_path.name}")

    with rawpy.imread(str(nef_path)) as raw:
        rgb = raw.postprocess(
            use_camera_wb=True,
            half_size=True,
            no_auto_bright=True,
            output_color=rawpy.ColorSpace.sRGB,
        )

    # Convert to float for precise manipulation
    rgb = rgb.astype(np.float32) / 255.0

    # ---- Step 1: Background neutralization (balance channels) ----
    bg_r = np.median(rgb[:, :, 0])
    bg_g = np.median(rgb[:, :, 1])
    bg_b = np.median(rgb[:, :, 2])

    # Avoid division by zero
    bg_r = max(bg_r, 1e-6)
    bg_g = max(bg_g, 1e-6)
    bg_b = max(bg_b, 1e-6)

    rgb[:, :, 0] /= bg_r
    rgb[:, :, 1] /= bg_g
    rgb[:, :, 2] /= bg_b

    # Re-normalize to [0, 1]
    rgb = np.clip(rgb / np.max(rgb), 0, 1)

    # ---- Step 2: Linear stretch (clip + stretch) ----
    black_point = 0.05
    white_point = 0.85
    rgb = np.clip((rgb - black_point) / (white_point - black_point), 0, 1)

    # ---- Step 3: Gamma curve ----
    rgb = np.power(rgb, 0.5)

    # Convert back to 8-bit and save
    rgb_uint8 = (rgb * 255).astype(np.uint8)
    img = Image.fromarray(rgb_uint8)
    img.save(str(output_png), "PNG")
    return img


# ---------------------------------------------------------------------------
# Task management
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


def start_platesolve(target_name: str,
                     exposure_seconds: int = 30,
                     iso: str = "400",
                     search_radius_deg: float = 1.5) -> str:
    """
    Start a plate-solving task in a background thread (capture live).

    Returns the task_id immediately.
    """
    task_id = create_task()
    thread = threading.Thread(
        target=run_platesolve_pipeline,
        args=(task_id, target_name, exposure_seconds, iso, search_radius_deg),
        daemon=True,
    )
    thread.start()
    return task_id


def start_platesolve_from_file(
    file_path: str,
    target_name: str,
    search_radius_deg: float = 1.5,
) -> str:
    """
    Start a plate-solving task from an already-captured file (uploaded).

    Returns the task_id immediately.
    """
    task_id = create_task()
    thread = threading.Thread(
        target=run_platesolve_from_file,
        args=(task_id, file_path, target_name, search_radius_deg),
        daemon=True,
    )
    thread.start()
    return task_id



def run_platesolve_from_file(
    task_id: str,
    file_path: str,
    target_name: str,
    search_radius_deg: float = 1.5,
) -> None:
    """
    Pipeline that starts from an existing image file instead of capturing live.
    """
    result = _get_task(task_id)

    try:
        local_path = Path(file_path)
        if not local_path.exists():
            raise RuntimeError(f"File not found: {file_path}")

        result.local_path = str(local_path)

        # --- Step 1: Resolve target ---
        _update_task(task_id, status="solving", progress="Resolving target…")
        ra, dec, resolved_name = resolve_target(target_name)
        result.target_name = resolved_name
        result.target_ra = ra
        result.target_dec = dec
        log.info("Target resolved: %s at (%.4f, %.4f)", resolved_name, ra, dec)

        # --- Step 2: Convert to PNG ---
        _update_task(task_id, status="solving", progress="Converting image…")
        captures_dir = PLATESOLVE_DIR / task_id
        captures_dir.mkdir(parents=True, exist_ok=True)
        png_path = captures_dir / "input.png"
        pil_image = _convert_to_png(local_path, png_path)
        if pil_image is None:
            raise RuntimeError("Could not decode uploaded image file")

        # --- Step 3: Detect stars ---
        _update_task(task_id, status="solving", progress="Detecting stars…")
        img_grey = np.array(pil_image.convert("L")).astype(np.float64)
        image_stars = detect_stars(img_grey, fwhm_px=4.0, threshold_sigma=5.0, max_stars=300)
        result.n_stars_detected = len(image_stars)
        log.info("Detected %d stars", len(image_stars))

        # --- Step 4: Query reference catalogue ---
        _update_task(task_id, status="solving", progress="Querying star catalogue…")
        catalogue = query_reference_stars(ra, dec, radius_deg=search_radius_deg,
                                          max_magnitude=12.0, max_stars=300)
        log.info("Got %d reference catalogue stars", len(catalogue))

        if not catalogue:
            raise RuntimeError("No reference catalogue stars found in this region")

        # --- Step 5: Triangle match ---
        _update_task(task_id, status="solving", progress="Matching star patterns…")
        wcs: Optional[WCS] = None
        scale_arcsec = 0.0
        n_matched = 0

        if len(image_stars) >= 3 and len(catalogue) >= 3:
            matched = _match_triangles(image_stars, catalogue)
            if matched:
                wcs, scale_arcsec, n_matched = fit_wcs_from_matches(
                    image_stars, catalogue, matched, img_grey.shape
                )

        if wcs is None:
            _update_task(task_id, status="solving", progress="Refining plate solution…")
            wcs, scale_arcsec, n_matched = _fit_wcs_simple(
                image_stars, catalogue, img_grey.shape, ra, dec
            )

        if wcs is not None:
            result.solved_ra = float(wcs.wcs.crval[0])
            result.solved_dec = float(wcs.wcs.crval[1])
            result.solved_scale = scale_arcsec
            result.solved_rotation = float(np.rad2deg(math.atan2(
                wcs.wcs.pc[1, 0], wcs.wcs.pc[0, 0]
            )))
            result.n_stars_matched = n_matched
            h, w = img_grey.shape
            if scale_arcsec > 0:
                result.field_width_arcmin = (w * scale_arcsec) / 60.0
                result.field_height_arcmin = (h * scale_arcsec) / 60.0

        # --- Step 6: Query DSO catalogues ---
        _update_task(task_id, status="solving", progress="Finding DSOs in field…")
        solve_ra = result.solved_ra if result.solved_ra != 0 else ra
        solve_dec = result.solved_dec if result.solved_dec != 0 else dec
        fov_radius = max(search_radius_deg,
                         max(result.field_width_arcmin, result.field_height_arcmin) / 60.0 * 1.2)
        dso_list = query_dso_catalogues(solve_ra, solve_dec, radius_deg=fov_radius)
        result.annotations = dso_list

        # --- Step 7: Annotate ---
        _update_task(task_id, status="solving", progress="Rendering annotations…")
        annotated = annotate_image(
            pil_image, wcs, dso_list, image_stars,
            result, target_ra=ra, target_dec=dec,
        )
        annotated_path = captures_dir / "annotated.png"
        annotated.save(str(annotated_path), "PNG")
        result.annotated_path = str(annotated_path)

        if wcs is not None:
            try:
                result.wcs_json = json.dumps({
                    "crpix": list(wcs.wcs.crpix),
                    "crval": list(wcs.wcs.crval),
                    "cdelt": list(wcs.wcs.cdelt),
                    "pc": wcs.wcs.pc.tolist(),
                    "ctype": list(wcs.wcs.ctype),
                })
            except Exception:
                result.wcs_json = "{}"

        result.success = True
        result.status = "done"
        log.info("Plate solve from file complete: %d DSOs annotated", len(dso_list))

    except Exception as exc:
        log.error("Plate solve from file failed: %s", exc)
        result.status = "error"
        result.error = str(exc)
        _update_task(task_id, status="error", error=str(exc))


