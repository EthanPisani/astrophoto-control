import subprocess
import os
import sys
import re
import csv
import shutil

# Third-party astronomy dependencies
try:
    import matplotlib
    # Force the non-interactive Agg backend BEFORE pyplot is imported.
    # Without this, matplotlib auto-selects an interactive backend (e.g.
    # TkAgg, since Tk is installed) and tries to create a GUI window.
    # When this module runs inside a Flask worker thread (not the main
    # thread), that fails with "main thread is not in main loop" /
    # "Tcl_AsyncDelete: async handler deleted by the wrong thread" and can
    # crash the whole process (IOT instruction / core dumped).
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    import rawpy
    from astropy.coordinates import SkyCoord
    from astropy import units as u
except ImportError:
    print("[-] Missing dependencies. Run: pip install astropy matplotlib rawpy")
    sys.exit(1)

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
# CSV files Siril ships (ngc.csv, ic.csv), read directly. No Siril
# installation or flatpak path required; point this at wherever you keep
# those files. Only used if ASTROCAP_DSO_SOURCE=csv (see below) — the
# default is "vizier", which needs no extracted files at all.
_DSO_CATALOG_DIR = Path(os.path.expanduser(
    os.environ.get("ASTROCAP_DSO_CATALOG_DIR", "~/src/sc-data/catalogs/siril")
))

# (filename, catalog_kind) — catalog_kind drives the colour split.
# The LDN layer is intentionally excluded because it obscures the chart
# instead of helping with bearings.
_DSO_CATALOG_FILES: list[tuple[str, str]] = [
    ("ngc.csv", "ngc"),
    ("ic.csv", "ic"),
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
_VIZIER_IC_CATALOG = "VII/114/ic"
_VIZIER_SH2_CATALOG = "VII/20/catalog"
_VIZIER_LDN_CATALOG = "VII/7A/ldn"


@dataclass(frozen=True)
class SirilRuntime:
    name: str
    command: tuple[str, ...]


def _detect_siril_runtime() -> SirilRuntime:
    siril_cli = shutil.which("siril-cli")
    if siril_cli:
        return SirilRuntime(
            name=f"Debian/package install ({siril_cli})",
            command=(siril_cli,),
        )

    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            probe = subprocess.run(
                [flatpak, "info", "org.siril.Siril"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if probe.returncode == 0:
                return SirilRuntime(
                    name="Flatpak (org.siril.Siril)",
                    command=(flatpak, "run", "--command=siril-cli", "org.siril.Siril"),
                )
        except Exception:
            pass

    return SirilRuntime(name="unavailable", command=())


_SIRIL_RUNTIME = _detect_siril_runtime()
if _SIRIL_RUNTIME.command:
    print(f"[*] Detected Siril runtime: {_SIRIL_RUNTIME.name}")
else:
    print("[-] Siril runtime not found. Install siril-cli or the Flatpak app org.siril.Siril.")

log = logging.getLogger("siril_annotate")


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
    catalog_kind: str = ""  # ngc | ic | messier | sh2 | ldn | stars
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


def _catalogue_label(catalog_kind: str, raw_name: str) -> str:
    compact = raw_name.strip().replace(" ", "")
    if not compact:
        return raw_name.strip()

    explicit_ic = re.match(r"^(?:IC|I)(\d+[A-Za-z]?)$", compact, re.IGNORECASE)
    if explicit_ic:
        return f"IC{explicit_ic.group(1)}"

    explicit_ngc = re.match(r"^(?:NGC|N)(\d+[A-Za-z]?)$", compact, re.IGNORECASE)
    if explicit_ngc:
        return f"NGC{explicit_ngc.group(1)}"

    explicit_messier = re.match(r"^(?:M|MESSIER)(\d+[A-Za-z]?)$", compact, re.IGNORECASE)
    if explicit_messier:
        return f"M{explicit_messier.group(1)}"

    prefix_map = {
        "ngc": "NGC",
        "ic": "IC",
        "messier": "M",
        "sh2": "Sh2",
        "ldn": "LdN",
        "stars": "Star",
    }
    prefix = prefix_map.get(catalog_kind.lower(), catalog_kind.upper())

    if compact.upper().startswith(prefix.upper()):
        return compact

    match = re.search(r"(\d+[A-Za-z]?)", compact)
    if match:
        return f"{prefix}{match.group(1)}"
    return f"{prefix}{compact}"


def _catalogue_kind_for_name(catalog_kind: str, raw_name: str) -> str:
    """Infer the most specific catalogue kind from a raw catalogue name."""
    catalog_kind = (catalog_kind or "").strip().lower()
    compact = (raw_name or "").strip().replace(" ", "")

    explicit_ic = re.match(r"^(?:IC|I)(\d+[A-Za-z]?)$", compact, re.IGNORECASE)
    if explicit_ic:
        return "ic"

    explicit_ngc = re.match(r"^(?:NGC|N)(\d+[A-Za-z]?)$", compact, re.IGNORECASE)
    if explicit_ngc:
        return "ngc"

    return catalog_kind


def _annotation_color(catalog_kind: str, obj_type: str) -> str:
    catalog_kind = (catalog_kind or "").strip().lower()
    obj_type = (obj_type or "").strip().lower()
    if catalog_kind == "ngc":
        return "#7CFF6B"
    if catalog_kind == "ic":
        return "#FFB347"
    if obj_type == "star":
        return "#FF5D73"
    return "#D6D6D6"


def _skycoord_to_pixel(center_coord: SkyCoord, obj_coord: SkyCoord, pixel_scale_deg: float,
                       rotation_deg: float, img_width: int, img_height: int) -> tuple[float, float]:
    dra, ddec = center_coord.spherical_offsets_to(obj_coord)
    dx_raw = -dra.deg / pixel_scale_deg
    dy_raw = ddec.deg / pixel_scale_deg

    if rotation_deg:
        theta = -np.radians(rotation_deg)
        cos_t, sin_t = np.cos(theta), np.sin(theta)
        dx = dx_raw * cos_t - dy_raw * sin_t
        dy = dx_raw * sin_t + dy_raw * cos_t
    else:
        dx, dy = dx_raw, dy_raw

    return (img_width / 2.0) + dx, (img_height / 2.0) - dy


@functools.lru_cache(maxsize=256)
def _resolve_star_coord(star_name: str) -> Optional[SkyCoord]:
    if not _HAS_ASTROQUERY:
        return None
    try:
        Simbad.add_votable_fields("ra", "dec")
        result = Simbad.query_object(star_name)
        if result is None or len(result) == 0:
            return None
        return SkyCoord(ra=float(result["ra"][0]), dec=float(result["dec"][0]), unit=u.deg)
    except Exception as exc:
        log.debug("Could not resolve constellation star %s: %s", star_name, exc)
        return None


_CONSTELLATION_LINE_SEGMENTS: dict[str, tuple[tuple[str, str], ...]] = {
    "Cyg": (
        ("Deneb", "Sadr"),
        ("Sadr", "Albireo"),
        ("Sadr", "Delta Cygni"),
        ("Sadr", "Epsilon Cygni"),
        ("Delta Cygni", "Zeta Cygni"),
        ("Albireo", "Kappa Cygni"),
    ),
    "Cep": (
        ("Alderamin", "Alfirk"),
        ("Alderamin", "Errai"),
        ("Alfirk", "Errai"),
    ),
    "Lyr": (
        ("Vega", "Sheliak"),
        ("Vega", "Sulafat"),
        ("Sheliak", "Sulafat"),
    ),
    "Aql": (
        ("Altair", "Tarazed"),
        ("Altair", "Alshain"),
    ),
    "Cas": (
        ("Caph", "Schedar"),
        ("Schedar", "Gamma Cassiopeiae"),
        ("Gamma Cassiopeiae", "Ruchbah"),
        ("Ruchbah", "Segin"),
        ("Segin", "Caph"),
    ),
    "Del": (
        ("Sualocin", "Rotanev"),
        ("Rotanev", "Delta Delphini"),
        ("Sualocin", "Delta Delphini"),
    ),
}


def _draw_constellation_overlay(ax, center_coord: SkyCoord, pixel_scale_deg: float,
                                rotation_deg: float, img_width: int, img_height: int) -> list[DsoAnnotation]:
    if not _HAS_ASTROQUERY:
        return []

    line_color = "#8DE8FF"
    label_color = "#B7F1FF"
    star_color = "#FFE08A"
    drawn_any = False
    star_points: dict[str, tuple[float, float, float, float]] = {}
    visible_star_points: dict[str, tuple[float, float, float, float]] = {}

    for constellation, segments in _CONSTELLATION_LINE_SEGMENTS.items():
        constellation_points: list[tuple[float, float]] = []
        for start_name, end_name in segments:
            start_coord = _resolve_star_coord(start_name)
            end_coord = _resolve_star_coord(end_name)
            if start_coord is None or end_coord is None:
                continue

            start_xy = _skycoord_to_pixel(center_coord, start_coord, pixel_scale_deg,
                                          rotation_deg, img_width, img_height)
            end_xy = _skycoord_to_pixel(center_coord, end_coord, pixel_scale_deg,
                                        rotation_deg, img_width, img_height)

            start_payload = (start_xy[0], start_xy[1], float(start_coord.ra.deg), float(start_coord.dec.deg))
            end_payload = (end_xy[0], end_xy[1], float(end_coord.ra.deg), float(end_coord.dec.deg))
            star_points[start_name] = start_payload
            star_points[end_name] = end_payload

            if -80.0 <= start_xy[0] <= img_width + 80.0 and -80.0 <= start_xy[1] <= img_height + 80.0:
                visible_star_points[start_name] = start_payload
            if -80.0 <= end_xy[0] <= img_width + 80.0 and -80.0 <= end_xy[1] <= img_height + 80.0:
                visible_star_points[end_name] = end_payload

            ax.plot(
                [start_xy[0], end_xy[0]],
                [start_xy[1], end_xy[1]],
                color=line_color,
                linewidth=0.9,
                alpha=0.65,
                solid_capstyle="round",
                zorder=2,
            )
            if -80.0 <= start_xy[0] <= img_width + 80.0 and -80.0 <= start_xy[1] <= img_height + 80.0:
                constellation_points.append(start_xy)
            if -80.0 <= end_xy[0] <= img_width + 80.0 and -80.0 <= end_xy[1] <= img_height + 80.0:
                constellation_points.append(end_xy)
            drawn_any = True

        if constellation_points:
            label_x = sum(point[0] for point in constellation_points) / len(constellation_points)
            label_y = sum(point[1] for point in constellation_points) / len(constellation_points)
            ax.text(
                label_x,
                label_y - 10,
                constellation,
                color=label_color,
                fontsize=6,
                weight="bold",
                alpha=0.7,
                ha="center",
                va="bottom",
                zorder=3,
            )

    for star_name, (x_pix, y_pix, ra_deg, dec_deg) in visible_star_points.items():
        ax.scatter([x_pix], [y_pix], s=12, color=star_color, edgecolors="none", zorder=3, alpha=0.9)
        ax.text(
            x_pix + 3,
            y_pix - 3,
            star_name,
            color=star_color,
            fontsize=5.5,
            weight="bold",
            alpha=0.9,
            ha="left",
            va="bottom",
            zorder=4,
        )

    if drawn_any:
        ax.text(0.02, 0.98, "Constellation lines", transform=ax.transAxes,
                color=label_color, fontsize=7, alpha=0.6, ha="left", va="top")

    return [
        DsoAnnotation(
            name=star_name,
            obj_type="star",
            catalog_kind="stars",
            ra=ra_deg,
            dec=dec_deg,
        )
        for star_name, (_, _, ra_deg, dec_deg) in visible_star_points.items()
    ]


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


# ---------------------------------------------------------------------------
# NGC / IC catalogue — live VizieR (default) or local CSVs
# ---------------------------------------------------------------------------

# catalog_id, catalog_kind, name-column candidates, ra column, dec column,
# ra unit, diameter-column candidates
_VIZIER_CATALOGS: list[tuple[str, str, tuple[str, ...], str, str, u.Unit, tuple[str, ...]]] = [
    (_VIZIER_NGC_CATALOG, "ngc", ("Name",), "RAB2000", "DEB2000", u.hourangle, ("size", "Size", "Diam")),
    (_VIZIER_IC_CATALOG, "ic", ("Name",), "RAJ2000", "DEJ2000", u.deg, ("size", "Size", "Diam")),
]


def _vizier_first_col(colnames: list[str], candidates: tuple[str, ...]) -> Optional[str]:
    for c in candidates:
        if c in colnames:
            return c
    return None


def _query_dso_vizier(ra_deg: float, dec_deg: float, radius_deg: float) -> list[DsoAnnotation]:
    """
    Query the live catalogues used for the large-object overlay.

    This intentionally keeps the map focused on NGC/IC objects rather than
    filling the frame with low-value background catalogues.
    """
    if not _HAS_ASTROQUERY:
        log.warning("astroquery not installed — cannot query VizieR/SIMBAD. "
                    "pip install astroquery, or set ASTROCAP_DSO_SOURCE=csv.")
        return []

    coord = SkyCoord(ra=ra_deg, dec=dec_deg, unit=u.deg)
    annotations: list[DsoAnnotation] = []

    for catalog_id, catalog_kind, name_cands, ra_col_want, dec_col_want, ra_unit, diam_cands in _VIZIER_CATALOGS:
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
                    inferred_kind = _catalogue_kind_for_name(catalog_kind, name)
                    diameter_arcmin = 0.0
                    if diam_col:
                        try:
                            v = float(row[diam_col])
                            if v > 0:
                                diameter_arcmin = v
                        except (ValueError, TypeError):
                            pass
                    annotations.append(DsoAnnotation(
                        name=_catalogue_label(inferred_kind, name),
                        obj_type="Gx",
                        catalog_kind=inferred_kind,
                        ra=float(sc.ra.deg), dec=float(sc.dec.deg),
                        diameter_arcmin=diameter_arcmin,
                    ))
                    n_parsed += 1
                except Exception:
                    continue

        log.info("VizieR %s: %d objects", catalog_id, n_parsed)

    log.info("VizieR: %d objects total within %.2f° of (%.4f, %.4f)",
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

    for filename, catalog_kind in _DSO_CATALOG_FILES:
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

                inferred_kind = _catalogue_kind_for_name(catalog_kind, name)

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
                    "catalog_kind": inferred_kind,
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
                name=_catalogue_label(obj["catalog_kind"], obj["name"]),
                obj_type="Gx",
                catalog_kind=obj["catalog_kind"],
                ra=obj["ra"], dec=obj["dec"],
                diameter_arcmin=obj["diameter_arcmin"],
            ))
    return annotations


def query_dso_catalogues(ra_deg: float, dec_deg: float,
                         radius_deg: float = 1.0) -> list[DsoAnnotation]:
    """
    Find catalogue objects within radius_deg of (ra_deg, dec_deg).

    ASTROCAP_DSO_SOURCE="vizier" (default): live NGC2000 + IC (VizieR) —
    the large-object catalogues used here for bearings, no extracted Siril
    files, requires internet.
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


def parse_siril_output_for_wcs(output_text: str) -> dict:
    """
    Parses the terminal output from Siril to extract center coordinates,
    pixel scale, true field camera rotation angle, and star counts.

    Produces both the raw sexagesimal 'ra'/'dec' strings (kept for
    backward compatibility with bake_png_annotations, which expects an
    hourangle-style string) AND decimal-degree 'ra_deg'/'dec_deg' floats
    (needed to populate PlateSolveResult.solved_ra/solved_dec, which are
    plain floats).
    """
    wcs_data: dict = {}

    ra_match = re.search(r"Image center: alpha:\s*([0-9hms\s\.\:]+)", output_text, re.IGNORECASE)
    dec_match = re.search(r"delta:\s*([0-9dms\s\.\:\+-]+)", output_text, re.IGNORECASE)
    scale_match = re.search(r"Resolution:\s*([0-9\.]+)\s*arcsec/pixel", output_text, re.IGNORECASE)

    # Matches: "log: Up is +271.95 deg CounterclockWise wrt. N"
    rotation_match = re.search(r"Up is\s*([\+\-0-9\.]+)\s*deg\s*CounterclockWise", output_text, re.IGNORECASE)

    # Star-count lines. Siril's exact wording can vary by version, so we
    # try a couple of common phrasings and fall back to 0 rather than
    # raising if none match.
    detected_match = (
        re.search(r"Found\s+(\d+)\s+star", output_text, re.IGNORECASE)
        or re.search(r"(\d+)\s+stars?\s+(?:were\s+)?detected", output_text, re.IGNORECASE)
    )
    matched_match = (
        re.search(r"(\d+)\s*/\s*(\d+)\s+stars?", output_text, re.IGNORECASE)
        or re.search(r"(\d+)\s+stars?\s+(?:were\s+)?(?:matched|used for the solution)", output_text, re.IGNORECASE)
    )

    if ra_match and dec_match:
        ra_str = ra_match.group(1).strip().replace(" ", ":")
        dec_str = dec_match.group(1).strip().replace(" ", ":")
        wcs_data["ra"] = ra_str
        wcs_data["dec"] = dec_str
        try:
            center = SkyCoord(ra_str, dec_str, unit=(u.hourangle, u.deg))
            wcs_data["ra_deg"] = float(center.ra.deg)
            wcs_data["dec_deg"] = float(center.dec.deg)
        except Exception as exc:
            log.warning("Could not convert solved RA/Dec %r / %r to decimal degrees: %s",
                        ra_str, dec_str, exc)

    if scale_match:
        wcs_data["scale"] = float(scale_match.group(1))

    if rotation_match:
        wcs_data["rotation"] = float(rotation_match.group(1))
    else:
        wcs_data["rotation"] = 0.0  # Fallback default

    if detected_match:
        wcs_data["n_stars_detected"] = int(detected_match.group(1))
    if matched_match:
        # "(\d+)/(\d+) stars" form has matched in group(1); the
        # single-count phrasing also captures the count in group(1).
        wcs_data["n_stars_matched"] = int(matched_match.group(1))
        if matched_match.lastindex and matched_match.lastindex >= 2 and "n_stars_detected" not in wcs_data:
            wcs_data["n_stars_detected"] = int(matched_match.group(2))

    return wcs_data

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


def bake_png_annotations(nef_path: str, output_png: str, center_ra: str, center_dec: str,
                          arcsec_per_pixel: float, rotation_deg: float):
    """
    Reads the RAW file, applies manual channel balancing to fix the green cast,
    performs a black-point background stretch, and renders rotated object markers.

    Returns (local_objects, img_width, img_height, half_size_scale) so the
    caller can compute field-of-view size and propagate the DSO list —
    previously these were computed here and then discarded.
    """
    print(f"[*] Correcting RAW color channels and sky background...")

    # 1. Read raw image matrix with customized processing profiles
    with rawpy.imread(nef_path) as raw:
        rgb = raw.postprocess(
            use_camera_wb=True,
            half_size=True,
            no_auto_bright=True,
            output_color=rawpy.ColorSpace.sRGB
        )

    rgb = rgb.astype(np.float32) / 255.0

    # STEP A: Neutralize the Green cast by balancing channels
    bg_r = np.median(rgb[:, :, 0])
    bg_g = np.median(rgb[:, :, 1])
    bg_b = np.median(rgb[:, :, 2])

    rgb[:, :, 0] /= bg_r
    rgb[:, :, 1] /= bg_g
    rgb[:, :, 2] /= bg_b

    rgb = np.clip(rgb / np.max(rgb), 0, 1)

    # STEP B: Linear stretch + gamma
    black_point = 0.05
    white_point = 0.85
    rgb = np.clip((rgb - black_point) / (white_point - black_point), 0, 1)
    rgb = np.power(rgb, 0.5)

    img_height, img_width, _ = rgb.shape
    base_img_height = img_height
    base_img_width = img_width
    # half_size=True in rawpy halves the pixel dimensions, so the
    # effective arcsec/pixel for *this* image is double what Siril
    # solved on the full-resolution frame.
    half_size_scale = arcsec_per_pixel * 2
    pixel_scale_deg = half_size_scale / 3600.0

    try:
        center_coord = SkyCoord(center_ra, center_dec, unit=(u.hourangle, u.deg))
    except Exception:
        center_coord = SkyCoord(center_ra, center_dec, unit=(u.deg, u.deg))

    search_radius = (max(img_width, img_height) * pixel_scale_deg) / 2.0

    local_objects = query_dso_catalogues(
        center_coord.ra.deg,
        center_coord.dec.deg,
        search_radius,
    )

    border_px_x = max(24, int(round(base_img_width * 0.05)))
    border_px_y = max(24, int(round(base_img_height * 0.05)))
    rgb = np.pad(
        rgb,
        ((border_px_y, border_px_y), (border_px_x, border_px_x), (0, 0)),
        mode="constant",
        constant_values=0.0,
    )
    img_height, img_width, _ = rgb.shape

    render_dpi = 300
    fig, ax = plt.subplots(
        figsize=(img_width / render_dpi, img_height / render_dpi),
        dpi=render_dpi,
        facecolor='black',
    )
    try:
        ax.imshow(rgb, origin='upper', interpolation='nearest')
        ax.set_facecolor('#000000')
        ax.set_position([0, 0, 1, 1])
        ax.set_xlim(0, img_width)
        ax.set_ylim(img_height, 0)
        ax.set_autoscale_on(False)
        ax.set_axis_off()

        for obj in local_objects:
            obj_coord = SkyCoord(ra=obj.ra, dec=obj.dec, unit=u.deg)
            x_pix, y_pix = _skycoord_to_pixel(
                center_coord,
                obj_coord,
                pixel_scale_deg,
                rotation_deg,
                img_width,
                img_height,
            )

            if 0 <= x_pix < img_width and 0 <= y_pix < img_height:
                layer_color = _annotation_color(obj.catalog_kind, obj.obj_type)

                if obj.diameter_arcmin > 0:
                    diameter_arcsec = obj.diameter_arcmin * 60.0
                    marker_radius_pixels = (diameter_arcsec / half_size_scale) / 2.0
                    marker_radius_pixels = max(marker_radius_pixels, 4.0 if obj.catalog_kind == "ic" else 5.0)
                else:
                    marker_radius_pixels = 4.0 if obj.catalog_kind == "ic" else 5.0

                circle = plt.Circle(
                    (x_pix, y_pix),
                    radius=marker_radius_pixels,
                    edgecolor=layer_color,
                    facecolor="none",
                    fill=False,
                    linewidth=1.0,
                    alpha=0.95,
                )
                ax.add_patch(circle)

                ax.text(x_pix + (marker_radius_pixels + 4), y_pix, obj.name, color=layer_color,
                        fontsize=6.5, weight='bold',
                        bbox=dict(facecolor='#111111', alpha=0.45, edgecolor='none', pad=1))

        star_annotations = _draw_constellation_overlay(
            ax=ax,
            center_coord=center_coord,
            pixel_scale_deg=pixel_scale_deg,
            rotation_deg=rotation_deg,
            img_width=img_width,
            img_height=img_height,
        )

        fig.savefig(output_png, pad_inches=0, dpi=render_dpi)
    finally:
        # Always close the figure, even if something above raises, so
        # long-running Flask workers don't leak figures/memory over time.
        plt.close(fig)

    return local_objects + star_annotations, base_img_width, base_img_height, half_size_scale

def run_flatpak_siril_annotation(config: dict):
    print(f"[*] Running Siril annotation pipeline with config: {config}")
    nef_path = os.path.abspath(config['input_nef'])
    if not os.path.exists(nef_path):
        raise FileNotFoundError(f"Source RAW file not found: {nef_path}")

    work_dir = os.path.dirname(nef_path)
    nef_filename = os.path.basename(nef_path)

    coords_arg = ""
    if config.get('ra') and config.get('dec'):
        coords_arg = f'"{config["ra"]},{config["dec"]}"'

    siril_script_content = f"""requires 1.2.0
cd "{work_dir}"
load "{nef_filename}"
platesolve {coords_arg} -focal={config['focal_length']} -pixelsize={config['pixel_size']}
close
"""

    script_file_path = os.path.join(work_dir, "single_image_solve.ssf")
    with open(script_file_path, "w") as f:
        f.write(siril_script_content)

    print(f"[*] Generated temporary script file at: {script_file_path}")

    if not _SIRIL_RUNTIME.command:
        raise RuntimeError("Siril runtime not found. Install siril-cli or the Flatpak app org.siril.Siril.")

    print(f"[*] Executing isolated plate solve via {_SIRIL_RUNTIME.name}...")

    siril_args = [*_SIRIL_RUNTIME.command, '-s', script_file_path]
    process = subprocess.Popen(siril_args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    captured_logs = []
    solved_successfully = False

    while True:
        line = process.stdout.readline()
        if not line:
            break
        sys.stdout.write(line)
        sys.stdout.flush()
        captured_logs.append(line)
        if "Plate solving succeeded" in line or "Siril solve succeeded" in line:
            solved_successfully = True

    process.wait()
    if os.path.exists(script_file_path):
        os.remove(script_file_path)

    if process.returncode == 0:
        full_log_text = "".join(captured_logs)
        wcs_metadata = parse_siril_output_for_wcs(full_log_text)

        solved_ra = wcs_metadata.get('ra', config['ra'])
        solved_dec = wcs_metadata.get('dec', config['dec'])

        fallback_scale = (config['pixel_size'] / config['focal_length']) * 206.265
        solved_scale = wcs_metadata.get('scale', fallback_scale)
        solved_rot = wcs_metadata.get('rotation', 0.0)

        # Decimal-degree center: prefer what Siril actually solved; if the
        # log couldn't be parsed, fall back to the target coordinates we
        # asked it to solve near (config['ra']/['dec'] are already decimal
        # degrees, coming from resolve_target upstream).
        solved_ra_deg = wcs_metadata.get('ra_deg')
        solved_dec_deg = wcs_metadata.get('dec_deg')
        if solved_ra_deg is None or solved_dec_deg is None:
            try:
                solved_ra_deg = float(config['ra'])
                solved_dec_deg = float(config['dec'])
            except (TypeError, ValueError):
                solved_ra_deg = 0.0
                solved_dec_deg = 0.0

        output_png_path = os.path.join(work_dir, config['output_image'])
        print(f"[*] Baking annotations onto PNG: {output_png_path}")
        print(f"[*] WCS Metadata: RA={solved_ra}, Dec={solved_dec}, Scale={solved_scale} arcsec/pixel, Rotation={solved_rot} deg")
        print(f"[*] nef_path: {nef_path}")

        local_objects, img_width, img_height, half_size_scale = bake_png_annotations(
            nef_path=nef_path,
            output_png=output_png_path,
            center_ra=solved_ra,
            center_dec=solved_dec,
            arcsec_per_pixel=solved_scale,
            rotation_deg=solved_rot
        )

        # Field-of-view size in arcmin — use half_size_scale, the pixel
        # scale bake_png_annotations actually rendered at (it compensates
        # for rawpy's half_size=True downsampling).
        field_width_arcmin = (img_width * half_size_scale) / 60.0
        field_height_arcmin = (img_height * half_size_scale) / 60.0

        dso_annotations = local_objects

        wcs_metadata["ra_deg"] = solved_ra_deg
        wcs_metadata["dec_deg"] = solved_dec_deg
        wcs_metadata["scale"] = solved_scale
        wcs_metadata["rotation"] = solved_rot
        wcs_metadata["field_width_arcmin"] = field_width_arcmin
        wcs_metadata["field_height_arcmin"] = field_height_arcmin
        wcs_metadata.setdefault("n_stars_detected", 0)
        wcs_metadata.setdefault("n_stars_matched", 0)

        print(f"\n[+] Processing complete! Vector visual layers mapped out.")
        print(f"[+] Annotated image saved to: {output_png_path}")
        return wcs_metadata, output_png_path, dso_annotations
    else:
        print(f"\n[-] Pipeline execution stopped or coordinate mismatch (Exit Code: {process.returncode})")
    return None, None, []

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




def run_platesolve_from_file_siril(
    task_id: str,
    file_path: str,
    target_name: str,
    search_radius_deg: float = 1.5,
    focal_length_mm: float = 135.0,
    pixel_size_um: float = 3.91,
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

        # --- Step 3: Plate solve + annotate via Siril ---
        _update_task(task_id, status="solving", progress="Plate solving…")
        wcs_metadata, output_path, dso_annotations = run_flatpak_siril_annotation(config={
            "input_nef": str(local_path),
            "output_image": "annotated.png",
            "focal_length": focal_length_mm,
            "pixel_size": pixel_size_um,
            "ra": f"{ra}",
            "dec": f"{dec}"
        })
        print(f"[*] WCS metadata: {wcs_metadata}")
        print(f"[*] Annotated output path: {output_path}")
        if wcs_metadata is None or output_path is None:
            raise RuntimeError("Siril annotation pipeline failed")

        # --- Step 4: Propagate solved metadata onto the result ---
        # This is the part that was previously being dropped entirely.
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

        result.wcs_json = "{}"  # Placeholder for now

        result.success = True
        result.status = "done"
        log.info(
            "Plate solve from file complete: center=(%.4f, %.4f) scale=%.3f\"/px "
            "rotation=%.2f field=%.1f'x%.1f' stars=%d/%d objects=%d",
            result.solved_ra, result.solved_dec, result.solved_scale,
            result.solved_rotation, result.field_width_arcmin, result.field_height_arcmin,
            result.n_stars_matched, result.n_stars_detected, len(result.annotations),
        )

    except Exception as exc:
        log.error("Plate solve from file failed: %s", exc)
        result.status = "error"
        result.error = str(exc)
        _update_task(task_id, status="error", error=str(exc))

def run_platesolve_pipeline_siril(task_id: str, target_name: str,
                            exposure_seconds: int = 30,
                            iso: str = "400",
                            search_radius_deg: float = 1.5,
                            focal_length_mm: float = 135.0,
                            pixel_size_um: float = 3.91) -> None:
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

       # --- Step 3: Plate solve + annotate via Siril ---
        _update_task(task_id, status="solving", progress="Plate solving…")
        wcs_metadata, output_path, dso_annotations = run_flatpak_siril_annotation(config={
            "input_nef": str(local_path),
            "output_image": "annotated.png",
            "focal_length": focal_length_mm,
            "pixel_size": pixel_size_um,
            "ra": f"{ra}",
            "dec": f"{dec}"
        })
        print(f"[*] WCS metadata: {wcs_metadata}")
        print(f"[*] Annotated output path: {output_path}")
        if wcs_metadata is None or output_path is None:
            raise RuntimeError("Siril annotation pipeline failed")

        # --- Step 4: Propagate solved metadata onto the result ---
        # This is the part that was previously being dropped entirely.
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

        result.wcs_json = "{}"  # Placeholder for now

        result.success = True
        result.status = "done"
        log.info(
            "Plate solve from file complete: center=(%.4f, %.4f) scale=%.3f\"/px "
            "rotation=%.2f field=%.1f'x%.1f' stars=%d/%d objects=%d",
            result.solved_ra, result.solved_dec, result.solved_scale,
            result.solved_rotation, result.field_width_arcmin, result.field_height_arcmin,
            result.n_stars_matched, result.n_stars_detected, len(result.annotations),
        )

    except Exception as exc:
        log.error("Plate solve from file failed: %s", exc)
        result.status = "error"
        result.error = str(exc)
        _update_task(task_id, status="error", error=str(exc))

def start_platesolve_siril(target_name: str,
                     exposure_seconds: int = 30,
                     iso: str = "400",
                     search_radius_deg: float = 1.5) -> str:
    """
    Start a plate-solving task in a background thread (capture live).

    Returns the task_id immediately.
    """
    task_id = create_task()
    thread = threading.Thread(
        target=run_platesolve_pipeline_siril,
        args=(task_id, target_name, exposure_seconds, iso, search_radius_deg),
        daemon=True,
    )
    thread.start()
    return task_id


        
def start_platesolve_from_file_siril(
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
        target=run_platesolve_from_file_siril,
        args=(task_id, file_path, target_name, search_radius_deg),
        daemon=True,
    )
    thread.start()
    return task_id


if __name__ == "__main__":
    pipeline_config = {
        "input_nef": "/media/ethan/Models/siril/2026-06-29/lights/4c48146d-2fc6-4f08-a97c-1ef8020d882b.nef", 
        "output_image": "flatpak_annotated_output.png", 
        "focal_length": 135,   
        "pixel_size": 3.91,    
        "ra": "20:57:14",      
        "dec": "43:55:09"
    }

    run_flatpak_siril_annotation(pipeline_config)
