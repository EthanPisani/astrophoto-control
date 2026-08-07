#!/usr/bin/env python3
"""
Build constellation_lines.csv entirely from online sources.

Downloads Stellarium's modern western skyculture definition (HIP-number
polylines) and cross-references with the HYG v4.1 star database to
resolve every HIP endpoint to an RA/Dec coordinate.

Sources (both CC BY-SA 4.0):
    Stellarium skycultures
        https://raw.githubusercontent.com/Stellarium/stellarium/refs/heads/master/skycultures/modern_st/index.json

    HYG Database v4.1
        https://raw.githubusercontent.com/astronexus/HYG-Database/main/hyg/CURRENT/hygdata_v41.csv

Output:
    constellation_lines.csv
    columns: constellation,hip1,ra1,dec1,hip2,ra2,dec2

This is a one-time build step — once the CSV exists, astap_annotate.py
and build_star_catalogs.py can consume it without any network access.
"""

from __future__ import annotations

import csv
import io
import json
import math
import sys
from pathlib import Path
from typing import Any

import requests

# ============================================================================
# Configuration
# ============================================================================

STELLARIUM_INDEX_URL = (
    "https://raw.githubusercontent.com/Stellarium/stellarium/"
    "refs/heads/master/skycultures/modern_st/index.json"
)

HYG_URL = (
    "https://raw.githubusercontent.com/astronexus/"
    "HYG-Database/main/hyg/CURRENT/hygdata_v41.csv"
)

# Default output path — build_star_catalogs.py reads from here.
OUTPUT_DIR = Path.home() / ".local_annotate" / "star-catalogs"
OUTPUT_CSV = OUTPUT_DIR / "constellation_lines.csv"

REQUEST_TIMEOUT = 120
MAG_LIMIT = 7.5  # Only include stars brighter than this


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


# ============================================================================
# Download HYG and build HIP → (ra_deg, dec_deg) lookup
# ============================================================================

def build_hip_coord_lookup() -> dict[int, tuple[float, float]]:
    """Download HYG v4.1 and return {hip: (ra_deg, dec_deg)}."""
    print(f"Downloading HYG v4.1...")
    print(f"  {HYG_URL}")

    resp = requests.get(HYG_URL, timeout=REQUEST_TIMEOUT, stream=True)
    resp.raise_for_status()

    buffer = io.StringIO()
    chunk_count = 0
    for chunk in resp.iter_content(1024 * 1024):
        if chunk:
            buffer.write(chunk.decode("utf-8", errors="replace"))
            chunk_count += 1

    print(f"  Downloaded ~{chunk_count} MB, parsing...")
    buffer.seek(0)

    reader = csv.reader(buffer)
    lookup: dict[int, tuple[float, float]] = {}
    count = 0
    skipped = 0

    for row in reader:
        if len(row) < 14:
            continue
        hip = _safe_int(row[1])
        if hip is None or hip == 0:
            continue
        mag = _safe_float(row[13])
        if mag is not None and mag > MAG_LIMIT:
            skipped += 1
            continue
        ra_hours = _safe_float(row[7])
        dec = _safe_float(row[8])
        if ra_hours is None or dec is None:
            continue
        lookup[hip] = (ra_hours * 15.0, dec)
        count += 1

    print(f"  Loaded {count:,} stars (mag <= {MAG_LIMIT}), skipped {skipped} too-faint")
    return lookup


# ============================================================================
# Download Stellarium index and extract constellation line segments
# ============================================================================

def parse_stellarium_index() -> list[tuple[str, list[tuple[int, int]]]]:
    """Download Stellarium index.json and return list of
    (constellation_abbr, [(hip1, hip2), ...]) for all line segments."""
    print(f"Downloading Stellarium skyculture index...")
    print(f"  {STELLARIUM_INDEX_URL}")

    resp = requests.get(STELLARIUM_INDEX_URL, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()

    constellations = data.get("constellations", [])
    print(f"  Found {len(constellations)} constellations")

    all_segments: list[tuple[str, list[tuple[int, int]]]] = []

    for con in constellations:
        con_id: str = con.get("id", "")
        # Extract abbreviation: "CON modern_st And" → "And"
        parts = con_id.split()
        abbr = parts[-1] if len(parts) >= 3 else con_id

        lines: list[list[int]] = con.get("lines", [])
        segments: list[tuple[int, int]] = []

        for polyline in lines:
            # Each polyline is [hip1, hip2, hip3, ...]
            # Consecutive pairs form segments
            for i in range(len(polyline) - 1):
                segments.append((polyline[i], polyline[i + 1]))

        all_segments.append((abbr, segments))

    total_segs = sum(len(segs) for _, segs in all_segments)
    print(f"  Extracted {total_segs} line segments")
    return all_segments


# ============================================================================
# Resolve coordinates and write CSV
# ============================================================================

def write_constellation_lines(
    constellations: list[tuple[str, list[tuple[int, int]]]],
    hip_coords: dict[int, tuple[float, float]],
) -> None:
    """Write constellation_lines.csv with resolved RA/Dec per endpoint."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    written = 0
    missing: set[int] = set()

    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["constellation", "hip1", "ra1", "dec1", "hip2", "ra2", "dec2"])

        for abbr, segments in constellations:
            for hip1, hip2 in segments:
                c1 = hip_coords.get(hip1)
                c2 = hip_coords.get(hip2)

                if c1 is None:
                    missing.add(hip1)
                if c2 is None:
                    missing.add(hip2)

                ra1 = f"{c1[0]:.6f}" if c1 else ""
                dec1 = f"{c1[1]:.6f}" if c1 else ""
                ra2 = f"{c2[0]:.6f}" if c2 else ""
                dec2 = f"{c2[1]:.6f}" if c2 else ""

                writer.writerow([abbr, str(hip1), ra1, dec1, str(hip2), ra2, dec2])
                written += 1

    print(f"  Wrote {written:,} segments → {OUTPUT_CSV}")

    if missing:
        print(f"  WARNING: {len(missing)} HIP numbers not found in HYG:")
        for hip in sorted(missing)[:20]:
            print(f"    HIP {hip}")
        if len(missing) > 20:
            print(f"    ... and {len(missing) - 20} more")
    else:
        print(f"  All HIP endpoints resolved successfully.")


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    print("=" * 60)
    print("CONSTELLATION LINES BUILDER")
    print("Stellarium skyculture + HYG v4.1 → constellation_lines.csv")
    print("=" * 60)

    # 1. Build HIP coordinate lookup from HYG
    hip_coords = build_hip_coord_lookup()

    # 2. Parse Stellarium constellation definitions
    constellations = parse_stellarium_index()

    # 3. Cross-reference and write CSV
    write_constellation_lines(constellations, hip_coords)

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
    except Exception as exc:
        print(f"\nERROR: {type(exc).__name__}: {exc}")
        sys.exit(1)
