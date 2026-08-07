#!/usr/bin/env python3
"""
Star catalog builder — downloads the HYG v4.1 database and cross-references
constellation line endpoint HIP numbers to produce a comprehensive
known_stars.csv with display names for EVERY star that appears as a
constellation-line vertex.

Online source:
    HYG Database v4.1 (astronexus/HYG-Database)
    https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv
    (CC BY-SA 4.0)

Output:
    ~/.local_annotate/star-catalogs/known_stars.csv

Column format (same as what astap_annotate.py already reads):
    name,hip,bayer,flam,con,ra,dec,mag

Name priority for display:
    1. Proper name (e.g. "Sirius", "Vega")
    2. Bayer designation in Latin + constellation (e.g. "Delta And")
    3. Flamsteed number + constellation (e.g. "21 And")
    4. "HIP NNNN" as absolute last resort

Usage:
    python3 build_star_catalogs.py
"""

from __future__ import annotations

import csv
import io
import math
import sys
from pathlib import Path

import requests

# ============================================================================
# Configuration
# ============================================================================

HYG_URL = (
    "https://raw.githubusercontent.com/astronexus/"
    "HYG-Database/main/hyg/CURRENT/hygdata_v41.csv"
)

# The constellation_lines.csv that astap_annotate.py already reads.
STAR_CATALOG_DIR = Path.home() / ".local_annotate" / "star-catalogs"
CONSTELLATION_LINES_CSV = STAR_CATALOG_DIR / "constellation_lines.csv"
KNOWN_STARS_CSV = STAR_CATALOG_DIR / "known_stars.csv"
KNOWN_STARS_BACKUP = STAR_CATALOG_DIR / "known_stars.csv.bak"

REQUEST_TIMEOUT = 120  # HYG is ~30 MB uncompressed

# Only include stars brighter than this magnitude (inclusive).
# Constellation line endpoints are all naked-eye stars, but we
# set a generous cutoff to be safe.
MAG_LIMIT = 7.5

# ============================================================================
# Bayer abbreviation → Latin name (typeable in any star map app)
# ============================================================================

_BAYER_LATIN: dict[str, str] = {
    "Alp": "Alpha",   "Bet": "Beta",    "Gam": "Gamma",   "Del": "Delta",
    "Eps": "Epsilon", "Zet": "Zeta",    "Eta": "Eta",     "The": "Theta",
    "Iot": "Iota",    "Kap": "Kappa",   "Lam": "Lambda",  "Mu":  "Mu",
    "Nu":  "Nu",      "Xi":  "Xi",      "Omi": "Omicron", "Pi":  "Pi",
    "Rho": "Rho",     "Sig": "Sigma",   "Tau": "Tau",     "Ups": "Upsilon",
    "Phi": "Phi",     "Chi": "Chi",     "Psi": "Psi",     "Ome": "Omega",
}

# Multi-letter Bayer suffixes that aren't Greek letters:
# "Alp-1" = α¹ Cen, "Alp-2" = α² Cen, etc.
# We'll handle number/letter suffixes below.

# ============================================================================
# Constellation abbreviation → full name
# ============================================================================

_CONSTELLATION: dict[str, str] = {
    "And": "And", "Ant": "Ant", "Aps": "Aps", "Aqr": "Aqr", "Aql": "Aql",
    "Ara": "Ara", "Ari": "Ari", "Aur": "Aur", "Boo": "Boo", "Cae": "Cae",
    "Cam": "Cam", "Cnc": "Cnc", "CVn": "CVn", "CMa": "CMa", "CMi": "CMi",
    "Cap": "Cap", "Car": "Car", "Cas": "Cas", "Cen": "Cen", "Cep": "Cep",
    "Cet": "Cet", "Cha": "Cha", "Cir": "Cir", "Col": "Col", "Com": "Com",
    "CrA": "CrA", "CrB": "CrB", "Crv": "Crv", "Crt": "Crt", "Cru": "Cru",
    "Cyg": "Cyg", "Del": "Del", "Dor": "Dor", "Dra": "Dra", "Equ": "Equ",
    "Eri": "Eri", "For": "For", "Gem": "Gem", "Gru": "Gru", "Her": "Her",
    "Hor": "Hor", "Hya": "Hya", "Hyi": "Hyi", "Ind": "Ind", "Lac": "Lac",
    "Leo": "Leo", "LMi": "LMi", "Lep": "Lep", "Lib": "Lib", "Lup": "Lup",
    "Lyn": "Lyn", "Lyr": "Lyr", "Men": "Men", "Mic": "Mic", "Mon": "Mon",
    "Mus": "Mus", "Nor": "Nor", "Oct": "Oct", "Oph": "Oph", "Ori": "Ori",
    "Pav": "Pav", "Peg": "Peg", "Per": "Per", "Phe": "Phe", "Pic": "Pic",
    "Psc": "Psc", "PsA": "PsA", "Pup": "Pup", "Pyx": "Pyx", "Ret": "Ret",
    "Sge": "Sge", "Sgr": "Sgr", "Sco": "Sco", "Scl": "Scl", "Sct": "Sct",
    "Ser": "Ser", "Sex": "Sex", "Tau": "Tau", "Tel": "Tel", "Tri": "Tri",
    "TrA": "TrA", "Tuc": "Tuc", "UMa": "UMa", "UMi": "UMi", "Vel": "Vel",
    "Vir": "Vir", "Vol": "Vol", "Vul": "Vul",
}


def _constellation_name(abbr: str) -> str:
    """Keep the HYG 3-letter abbreviation as the constellation name."""
    return abbr.strip()


# ============================================================================
# Name construction
# ============================================================================

def _build_bayer_name(raw_bayer: str, con: str) -> str | None:
    """Convert HYG bayer field to a typeable Latin Bayer designation.

    Examples:
        "Alp"       → "Alpha And"
        "Alp-1"     → "Alpha1 And"
        "The"       → "Theta And"
        "Ome"       → "Omega And"
    """
    bayer = (raw_bayer or "").strip()
    if not bayer:
        return None

    parts = bayer.split("-", 1)
    greek_code = parts[0]
    suffix = parts[1] if len(parts) > 1 else ""

    latin = _BAYER_LATIN.get(greek_code)
    if latin is None:
        return None

    if suffix:
        return f"{latin}{suffix} {con}"
    return f"{latin} {con}"


def _build_flamsteed_name(raw_flam: str, con: str) -> str | None:
    """Convert HYG flam field to "NN And"."""
    flam = (raw_flam or "").strip()
    if not flam:
        return None
    try:
        num = int(flam)
    except ValueError:
        return None
    if num <= 0:
        return None
    return f"{num} {con}"


def _build_display_name(proper: str, bayer: str, flam: str, con: str, hip: int) -> str:
    """Priority: proper name → Bayer designation → Flamsteed → 'HIP NNNN'."""
    proper = (proper or "").strip()
    if proper:
        return proper

    bayer_name = _build_bayer_name(bayer, con)
    if bayer_name:
        return bayer_name

    flam_name = _build_flamsteed_name(flam, con)
    if flam_name:
        return flam_name

    return f"HIP {hip}"


# ============================================================================
# HYG column indices (the CSV has NO header row)
# ============================================================================
# From: https://github.com/astronexus/HYG-Database
#
#  0: id
#  1: hip          ← Hipparcos catalog number
#  2: hd
#  3: hr
#  4: gl
#  5: bf           ← Bayer-Flamsteed designation
#  6: proper       ← proper name (e.g. "Sirius")
#  7: ra           ← right ascension (decimal HOURS — multiply by 15 for degrees)
#  8: dec          ← declination (decimal degrees)
#  9: dist
# 10: pmra
# 11: pmdec
# 12: rv
# 13: mag          ← apparent magnitude
# 14: absmag
# 15: spect
# 16: ci
# 17: x
# 18: y
# 19: z
# 20: vx
# 21: vy
# 22: vz
# 23: rarad
# 24: decrad
# 25: pmrarad
# 26: pmdecrad
# 27: bayer        ← Bayer designation (e.g. "Alp")
# 28: flam         ← Flamsteed number
# 29: con          ← constellation abbreviation
# 30: comp
# 31: comp_primary
# 32: base
# 33: lum
# 34: var
# 35: var_min
# 36: var_max

# HYG is ~120k rows; we load it into a dict keyed by HIP for O(1) lookup.
# Rows without a HIP number are skipped (e.g. the Sun, row 0).


def _safe_float(value: str) -> float | None:
    try:
        f = float(value.strip())
        if math.isfinite(f):
            return f
    except (ValueError, TypeError):
        pass
    return None


def _safe_int(value: str) -> int | None:
    try:
        return int(float(value.strip()))
    except (ValueError, TypeError):
        return None


def download_and_parse_hyg() -> dict[int, dict]:
    """Download HYG v4.1 and return dict {hip: {proper, bayer, flam, con, ra, dec, mag}}."""
    print(f"Downloading HYG v4.1 database...")
    print(f"  {HYG_URL}")

    resp = requests.get(HYG_URL, timeout=REQUEST_TIMEOUT, stream=True)
    resp.raise_for_status()

    # Stream the CSV and build the lookup
    print("  Parsing HYG (streaming, ~120k stars)...")
    hyg: dict[int, dict] = {}
    buffer = io.StringIO()
    chunk_count = 0

    for chunk in resp.iter_content(1024 * 1024):
        if chunk:
            buffer.write(chunk.decode("utf-8", errors="replace"))
            chunk_count += 1

    print(f"  Downloaded ~{chunk_count} MB, parsing...")
    buffer.seek(0)

    reader = csv.reader(buffer)
    count = 0
    skipped_no_hip = 0
    skipped_dim = 0
    skipped_mag = 0

    for row in reader:
        if len(row) < 14:
            continue  # malformed row

        hip = _safe_int(row[1])
        if hip is None or hip == 0:
            skipped_no_hip += 1
            continue

        mag = _safe_float(row[13])
        if mag is None or mag > MAG_LIMIT:
            skipped_mag += 1
            continue

        ra_hours = _safe_float(row[7])
        dec = _safe_float(row[8])
        if ra_hours is None or dec is None:
            skipped_dim += 1
            continue

        # HYG column 7 is RA in decimal HOURS — convert to degrees
        ra_deg = ra_hours * 15.0

        hyg[hip] = {
            "proper": (row[6] or "").strip(),
            "bayer": (row[27] or "").strip() if len(row) > 27 else "",
            "flam": (row[28] or "").strip() if len(row) > 28 else "",
            "con": (row[29] or "").strip() if len(row) > 29 else "",
            "ra": ra_deg,
            "dec": dec,
            "mag": mag,
        }
        count += 1

    print(f"  Loaded {count:,} stars (mag ≤ {MAG_LIMIT})")
    print(f"  Skipped: {skipped_no_hip} no-HIP, {skipped_mag} too-faint, {skipped_dim} no-coords")
    return hyg


# ============================================================================
# Load constellation line HIPs
# ============================================================================

def load_constellation_hips() -> set[int]:
    """Extract all unique HIP numbers from constellation_lines.csv."""
    if not CONSTELLATION_LINES_CSV.exists():
        print(f"ERROR: constellation_lines.csv not found at {CONSTELLATION_LINES_CSV}")
        sys.exit(1)

    hips: set[int] = set()
    with CONSTELLATION_LINES_CSV.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            for key in ("hip1", "hip2"):
                try:
                    hips.add(int(row[key]))
                except (KeyError, ValueError):
                    continue

    print(f"Constellation line endpoints: {len(hips)} unique HIP numbers")
    return hips


# ============================================================================
# Build enhanced known_stars.csv
# ============================================================================

def build_known_stars(hyg: dict[int, dict], target_hips: set[int]) -> list[dict]:
    """Build star entries for every constellation-line HIP."""
    stars: list[dict] = []
    missing_from_hyg: list[int] = []

    for hip in sorted(target_hips):
        entry = hyg.get(hip)
        if entry is None:
            missing_from_hyg.append(hip)
            continue

        display_name = _build_display_name(
            entry["proper"], entry["bayer"], entry["flam"], entry["con"], hip,
        )

        stars.append({
            "name": display_name,
            "hip": str(hip),
            "bayer": entry["bayer"],
            "flam": entry["flam"],
            "con": entry["con"],
            "ra": f"{entry['ra']:.6f}",
            "dec": f"{entry['dec']:.6f}",
            "mag": f"{entry['mag']:.2f}",
        })

    if missing_from_hyg:
        print(f"\nWARNING: {len(missing_from_hyg)} HIPs not found in HYG (mag ≤ {MAG_LIMIT}):")
        for hip in sorted(missing_from_hyg)[:20]:
            print(f"  HIP {hip}")
        if len(missing_from_hyg) > 20:
            print(f"  ... and {len(missing_from_hyg) - 20} more")

    return stars


# ============================================================================
# Write
# ============================================================================

def write_known_stars(stars: list[dict]) -> None:
    """Write known_stars.csv (backup existing first)."""
    STAR_CATALOG_DIR.mkdir(parents=True, exist_ok=True)

    # Backup existing
    if KNOWN_STARS_CSV.exists():
        print(f"\nBacking up existing → {KNOWN_STARS_BACKUP}")
        KNOWN_STARS_BACKUP.write_text(KNOWN_STARS_CSV.read_text())

    fields = ["name", "hip", "bayer", "flam", "con", "ra", "dec", "mag"]
    with KNOWN_STARS_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for star in stars:
            writer.writerow(star)

    print(f"Wrote {len(stars):,} stars → {KNOWN_STARS_CSV}")

    # Statistics
    proper_named = sum(1 for s in stars if not s["name"].startswith("HIP ") and not s["name"][0].isdigit())
    bayer_named = sum(1 for s in stars if s["bayer"] and not s["name"].startswith("HIP "))
    flamsteed_named = sum(1 for s in stars if s["flam"] and not s["bayer"] and not s["name"].startswith("HIP "))
    hip_fallback = sum(1 for s in stars if s["name"].startswith("HIP "))

    print(f"\nName breakdown:")
    print(f"  Proper names:       {proper_named}")
    print(f"  Bayer designations: {bayer_named}")
    print(f"  Flamsteed numbers:  {flamsteed_named}")
    print(f"  HIP fallback:       {hip_fallback}")


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    print("=" * 60)
    print("STAR CATALOG BUILDER")
    print("HYG v4.1 → constellation line endpoints → known_stars.csv")
    print("=" * 60)

    # 1. Load constellation line HIPs
    target_hips = load_constellation_hips()

    # 2. Download & parse HYG
    hyg = download_and_parse_hyg()

    # 3. Build enhanced catalog
    stars = build_known_stars(hyg, target_hips)

    # 4. Write
    write_known_stars(stars)

    print("\nDone — restart astap_annotate.py to pick up new star names.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        sys.exit(1)
