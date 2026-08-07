
#!/usr/bin/env python3
"""
DSO catalog generator.

Builds local NGC / IC / Messier catalogs using several astronomical
catalog sources, in order of preference:

    1. OpenNGC
    2. SIMBAD
    3. Saguaro Astronomy Club Deep Sky Database 8.1
    4. VizieR NGC 2000.0
    5. VizieR Sharpless HII Regions (Sh2) -- max angular diameters
    6. Curated large-DSO size overrides (large_dso_overrides.csv)

The final application CSV files are:

    messier.csv
    ngc.csv
    ic.csv

Format:

    name,ra,dec,diameter

where:

    ra       = decimal degrees
    dec      = decimal degrees
    diameter = angular major/largest dimension in arcminutes

A richer:

    dso-master.csv

is also written with:

    name
    ra
    dec
    diameter
    minor_diameter
    position_angle
    diameter_source
    diameter_quality
    object_type
    simbad_id

IMPORTANT:

OpenNGC MajAx / MinAx are in ARCMINUTES (not degrees).

They are used directly.

SIMBAD dimensions are also in arcminutes.

SAC dimensions are converted from the SAC format to arcminutes.

NGC 2000.0 size is already in arcminutes.

The generator only asks SIMBAD about objects whose OpenNGC size is
missing.

Sources:

OpenNGC:
    https://github.com/mattiaverga/OpenNGC

SIMBAD:
    https://simbad.cds.unistra.fr/

Saguaro Astronomy Club:
    https://www.saguaroastro.org/sac-downloads/

NGC 2000.0:
    https://vizier.cds.unistra.fr/viz-bin/VizieR-3?-source=VII%2F118%2Fngc2000
"""

from __future__ import annotations

import csv
import io
import math
import re
import sys
import tempfile
import time
import zipfile

from pathlib import Path
from typing import Iterable

import requests


# ============================================================================
# Configuration
# ============================================================================

NGC_URL = (
    "https://raw.githubusercontent.com/mattiaverga/"
    "OpenNGC/master/database_files/NGC.csv"
)

ADDENDUM_URL = (
    "https://raw.githubusercontent.com/mattiaverga/"
    "OpenNGC/master/database_files/addendum.csv"
)

# Saguaro Astronomy Club Deep Sky Database v8.1.
#
# This is the direct archive historically published by SAC.
#
# The archive contains:
#
#     SAC_DeepSky_81_QCQ.TXT
#     SAC_DeepSky_81_FENCE.TXT
#     SACDOC.TXT
#     etc.
#
SAC_URL = (
    "https://www.saguaroastro.org/"
    "wp-content/sac-docs/ObservingDownloads/"
    "SAC_DeepSky_ver81.zip"
)

# VizieR NGC 2000.0.
VIZIER_NGC2000_URL = (
    "https://vizier.cds.unistra.fr/viz-bin/asu-tsv"
    "?-source=VII/118/ngc2000"
    "&-out.max=20000"
    "&-out=Name,size"
)

# VizieR Sharpless HII Regions (Sh2) -- max angular diameters for 313 HII regions.
# Published: Sharpless S. 1959, ApJS 4, 257
SHARPLESS_URL = (
    "https://vizier.cds.unistra.fr/viz-bin/asu-tsv"
    "?-source=VII/20/catalog"
    "&-out.max=500"
    "&-out=Sh2,Diam"
    "&-out.form=mini"
)


# ============================================================================
# Paths
# ============================================================================

DATA_DIR = (
    Path.home()
    / ".local_annotate"
    / "dso-catalogs"
)

RAW_DIR = DATA_DIR / "raw"

NGC_RAW = RAW_DIR / "NGC.csv"
ADDENDUM_RAW = RAW_DIR / "addendum.csv"

SAC_RAW = RAW_DIR / "SAC_DeepSky_ver81.zip"
SAC_EXTRACTED = RAW_DIR / "sac81"

NGC2000_RAW = RAW_DIR / "ngc2000.tsv"

SHARPLESS_RAW = RAW_DIR / "sharpless.tsv"
SH2_NGC_CROSSREF = DATA_DIR / "sh2_ngc_crossref.csv"

MASTER_CSV = DATA_DIR / "dso-master.csv"

MESSIER_CSV = DATA_DIR / "messier.csv"
NGC_CSV = DATA_DIR / "ngc.csv"
IC_CSV = DATA_DIR / "ic.csv"


# ============================================================================
# Runtime settings
# ============================================================================

REQUEST_TIMEOUT = 90

SIMBAD_BATCH_SIZE = 200

SIMBAD_BATCH_DELAY = 0.75

# Coordinate matching tolerance.

# 30 arcseconds is deliberately conservative. NGC/IC catalog coordinates
# are usually much closer than this.
COORD_MATCH_RADIUS_DEG = (
    30.0 / 3600.0
)

DEG_TO_ARCMIN = 60.0


# ============================================================================
# General utilities
# ============================================================================

def clean_string(value) -> str:
    if value is None:
        return ""

    try:
        if hasattr(value, "mask") and bool(value.mask):
            return ""
    except Exception:
        pass

    text = str(value).strip()

    if text in {
        "",
        "--",
        "---",
        "None",
        "none",
        "NULL",
        "null",
        "nan",
        "NaN",
    }:
        return ""

    return text


def safe_float(value) -> float | None:
    text = clean_string(value)

    if not text:
        return None

    try:
        result = float(text)

        if not math.isfinite(result):
            return None

        return result

    except (ValueError, TypeError):
        return None


def positive_float(value) -> float | None:
    result = safe_float(value)

    if result is None:
        return None

    if result <= 0:
        return None

    return result


def chunked(
    values: list,
    size: int,
) -> Iterable[list]:
    for i in range(
        0,
        len(values),
        size,
    ):
        yield values[
            i:i + size
        ]


# ============================================================================
# HTTP downloading
# ============================================================================

def download_file(
    url: str,
    destination: Path,
) -> None:

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    print()
    print("Downloading:")
    print(f"  {url}")
    print(f"  -> {destination}")

    response = requests.get(
        url,
        timeout=REQUEST_TIMEOUT,
        headers={
            "User-Agent": (
                "LocalAnnotate-DSO-Catalog/1.0 "
                "astronomy catalog builder"
            )
        },
    )

    response.raise_for_status()

    if not response.content:
        raise RuntimeError(
            f"Downloaded file is empty: {url}"
        )

    temporary = destination.with_suffix(
        destination.suffix + ".tmp"
    )

    temporary.write_bytes(
        response.content
    )

    temporary.replace(
        destination
    )

    print(
        f"  {len(response.content):,} bytes"
    )


# ============================================================================
# Coordinate conversion
# ============================================================================

def sexagesimal_ra_to_deg(
    value: str,
) -> float | None:

    value = clean_string(value)

    if not value:
        return None

    try:
        parts = value.split(":")

        if len(parts) != 3:
            return None

        h = float(parts[0])
        m = float(parts[1])
        s = float(parts[2])

        result = (
            h
            + m / 60.0
            + s / 3600.0
        ) * 15.0

        if not math.isfinite(result):
            return None

        return result

    except (ValueError, TypeError):
        return None


def sexagesimal_dec_to_deg(
    value: str,
) -> float | None:

    value = clean_string(value)

    if not value:
        return None

    try:
        sign = (
            -1.0
            if value.startswith("-")
            else 1.0
        )

        value = value.lstrip("+-")

        parts = value.split(":")

        if len(parts) != 3:
            return None

        d = float(parts[0])
        m = float(parts[1])
        s = float(parts[2])

        result = sign * (
            d
            + m / 60.0
            + s / 3600.0
        )

        if not math.isfinite(result):
            return None

        return result

    except (ValueError, TypeError):
        return None


def angular_distance_deg(
    ra1: float,
    dec1: float,
    ra2: float,
    dec2: float,
) -> float:
    """
    Great-circle angular separation in degrees.
    """

    ra1_rad = math.radians(ra1)
    dec1_rad = math.radians(dec1)

    ra2_rad = math.radians(ra2)
    dec2_rad = math.radians(dec2)

    sin_ddec = math.sin(
        (dec2_rad - dec1_rad) / 2.0
    )

    sin_dra = math.sin(
        (ra2_rad - ra1_rad) / 2.0
    )

    a = (
        sin_ddec ** 2
        + math.cos(dec1_rad)
        * math.cos(dec2_rad)
        * sin_dra ** 2
    )

    a = max(
        0.0,
        min(1.0, a),
    )

    return math.degrees(
        2.0 * math.asin(
            math.sqrt(a)
        )
    )


# ============================================================================
# Catalog-name normalization
# ============================================================================

def normalize_catalog_name(
    value: str,
) -> str | None:

    text = (
        clean_string(value)
        .upper()
        .strip()
    )

    if not text:
        return None

    # Remove punctuation and whitespace.
    text = re.sub(
        r"[^A-Z0-9]",
        "",
        text,
    )

    # NGC
    match = re.fullmatch(
        r"NGC0*(\d+)",
        text,
    )

    if match:
        return (
            f"NGC{int(match.group(1))}"
        )

    # N prefix used by NGC 2000.
    match = re.fullmatch(
        r"N0*(\d+)",
        text,
    )

    if match:
        return (
            f"NGC{int(match.group(1))}"
        )

    # IC
    match = re.fullmatch(
        r"IC0*(\d+)",
        text,
    )

    if match:
        return (
            f"IC{int(match.group(1))}"
        )

    # I prefix used by some catalogs.
    match = re.fullmatch(
        r"I0*(\d+)",
        text,
    )

    if match:
        return (
            f"IC{int(match.group(1))}"
        )

    return None


# ============================================================================
# OpenNGC
# ============================================================================

def load_openngc(
    path: Path,
) -> list[dict[str, str]]:

    with path.open(
        "r",
        encoding="utf-8",
        newline="",
    ) as fh:

        reader = csv.DictReader(
            fh,
            delimiter=";",
        )

        rows = list(reader)

    if not rows:
        raise RuntimeError(
            f"Empty OpenNGC file: {path}"
        )

    required = {
        "Name",
        "RA",
        "Dec",
        "MajAx",
        "MinAx",
        "M",
    }

    missing = (
        required
        - set(rows[0].keys())
    )

    if missing:
        raise RuntimeError(
            "OpenNGC is missing columns: "
            + ", ".join(
                sorted(missing)
            )
        )

    return rows


def openngc_size(
    value,
) -> float | None:

    arcmin = positive_float(
        value
    )

    if arcmin is None:
        return None

    return arcmin


def make_openngc_object(
    row: dict[str, str],
) -> dict | None:

    name = clean_string(
        row.get("Name")
    )

    if not name:
        return None

    object_type = clean_string(
        row.get("Type")
    )

    if object_type in {
        "Dup",
        "NonEx",
    }:
        return None

    ra = sexagesimal_ra_to_deg(
        row.get("RA")
    )

    dec = sexagesimal_dec_to_deg(
        row.get("Dec")
    )

    if ra is None or dec is None:
        return None

    major = openngc_size(
        row.get("MajAx")
    )

    minor = openngc_size(
        row.get("MinAx")
    )

    return {
        "name": name,
        "normalized_name": (
            normalize_catalog_name(
                name
            )
        ),

        "ra": ra,
        "dec": dec,

        "diameter": major,
        "minor_diameter": minor,
        "position_angle": None,

        "diameter_source": (
            "OpenNGC"
            if major is not None
            else ""
        ),

        "diameter_quality": (
            "catalog"
            if major is not None
            else ""
        ),

        "object_type": object_type,

        "simbad_id": "",
    }


def build_openngc_objects(
    rows: list[dict[str, str]],
) -> dict[str, dict]:

    objects = {}

    for row in rows:

        obj = make_openngc_object(
            row
        )

        if obj is None:
            continue

        name = obj["name"]

        objects[name] = obj

    return objects


# ============================================================================
# SIMBAD
# ============================================================================

def query_simbad(
    names: list[str],
) -> dict[str, dict]:

    if not names:
        return {}

    try:
        from astroquery.simbad import Simbad
    except ImportError:
        print("\nSIMBAD: astroquery not installed -- skipping SIMBAD queries.")
        print("  Install with:  python3 -m pip install astroquery astropy")
        return {}

    simbad = Simbad(
        timeout=300
    )

    simbad.ROW_LIMIT = -1

    simbad.add_votable_fields(
        "dim"
    )

    result_map = {}

    total_batches = math.ceil(
        len(names)
        / SIMBAD_BATCH_SIZE
    )

    print()
    print(
        f"SIMBAD: querying "
        f"{len(names):,} missing objects "
        f"in {total_batches} batches"
    )

    for batch_number, batch in enumerate(
        chunked(
            names,
            SIMBAD_BATCH_SIZE,
        ),
        1,
    ):

        print(
            f"  Batch "
            f"{batch_number}/{total_batches} "
            f"({len(batch)} objects)"
        )

        try:

            table = simbad.query_objects(
                batch,
                async_job=True,
            )

        except Exception as exc:

            print(
                f"    FAILED: {exc}"
            )

            continue

        if table is None:
            print(
                "    No response."
            )
            continue

        recovered = 0

        for row in table:

            requested = clean_string(
                row["user_specified_id"]
            )

            if not requested:
                continue

            major = positive_float(
                row["galdim_majaxis"]
            )

            minor = positive_float(
                row["galdim_minaxis"]
            )

            angle = safe_float(
                row["galdim_angle"]
            )

            if major is None:
                continue

            result_map[
                normalize_catalog_name(
                    requested
                )
                or requested
            ] = {
                "major": major,
                "minor": minor,
                "angle": angle,
                "quality": clean_string(
                    row["galdim_qual"]
                ),
                "wavelength": clean_string(
                    row[
                        "galdim_wavelength"
                    ]
                ),
                "bibcode": clean_string(
                    row[
                        "galdim_bibcode"
                    ]
                ),
                "main_id": clean_string(
                    row["main_id"]
                ),
            }

            recovered += 1

        print(
            f"    recovered "
            f"{recovered:,}"
        )

        time.sleep(
            SIMBAD_BATCH_DELAY
        )

    print(
        f"SIMBAD total recovered: "
        f"{len(result_map):,}"
    )

    return result_map


def apply_simbad(
    objects: dict[str, dict],
    results: dict[str, dict],
) -> int:

    updated = 0

    for obj in objects.values():

        if obj["diameter"] is not None:
            continue

        key = obj[
            "normalized_name"
        ]

        if not key:
            continue

        result = results.get(
            key
        )

        if result is None:
            continue

        obj["diameter"] = (
            result["major"]
        )

        obj["minor_diameter"] = (
            result["minor"]
        )

        obj["position_angle"] = (
            result["angle"]
        )

        obj["diameter_source"] = (
            "SIMBAD"
        )

        quality = result[
            "quality"
        ]

        wavelength = result[
            "wavelength"
        ]

        if quality and wavelength:
            obj["diameter_quality"] = (
                f"{quality}/{wavelength}"
            )
        elif quality:
            obj["diameter_quality"] = (
                quality
            )
        else:
            obj["diameter_quality"] = (
                "SIMBAD"
            )

        obj["simbad_id"] = (
            result["main_id"]
        )

        updated += 1

    return updated


# ============================================================================
# Saguaro Astronomy Club
# ============================================================================

def extract_sac_archive() -> Path:

    SAC_EXTRACTED.mkdir(
        parents=True,
        exist_ok=True,
    )

    expected = (
        SAC_EXTRACTED
        / "SAC_DeepSky_81_QCQ.TXT"
    )

    if expected.exists():
        return expected

    print()
    print(
        "Extracting Saguaro Astronomy Club "
        "Deep Sky Database..."
    )

    with zipfile.ZipFile(
        SAC_RAW,
        "r",
    ) as archive:

        members = archive.namelist()

        target = None

        for member in members:

            filename = (
                Path(member).name
            )

            if filename.upper() == (
                "SAC_DEEPSKY_81_QCQ.TXT"
            ):
                target = member
                break

        if target is None:

            for member in members:

                filename = (
                    Path(member).name
                )

                if filename.upper().endswith(
                    "_QCQ.TXT"
                ):
                    target = member
                    break

        if target is None:
            raise RuntimeError(
                "Could not find SAC QCQ "
                "database inside archive.\n"
                "Archive contains:\n"
                + "\n".join(
                    members
                )
            )

        archive.extract(
            target,
            SAC_EXTRACTED,
        )

        extracted = (
            SAC_EXTRACTED
            / target
        )

        extracted.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if extracted != expected:
            extracted.replace(
                expected
            )

    return expected


def detect_sac_delimiter(
    text: str,
) -> str:

    # SAC calls its quote/comma/quote format QCQ.
    #
    # It should be comma-delimited, but we detect it rather than relying
    # blindly on the filename.

    sample = "\n".join(
        text.splitlines()[:20]
    )

    comma_count = sample.count(
        ","
    )

    pipe_count = sample.count(
        "|"
    )

    if comma_count >= pipe_count:
        return ","

    return "|"


def find_sac_header(
    lines: list[str],
) -> tuple[int, list[str]]:

    for index, line in enumerate(
        lines
    ):

        clean = line.strip()

        if not clean:
            continue

        delimiter = (
            ","
            if "," in clean
            else "|"
        )

        fields = [
            field.strip(
                ' "\t\r\n'
            )
            for field in clean.split(
                delimiter
            )
        ]

        lowered = [
            field.lower()
            for field in fields
        ]

        # SAC versions have used slightly different column names.
        has_name = any(
            field in {
                "name",
                "object",
                "id",
                "object name",
            }
            for field in lowered
        )

        has_size = any(
            "size" in field
            for field in lowered
        )

        if has_name and has_size:
            return (
                index,
                fields,
            )

    raise RuntimeError(
        "Could not automatically find "
        "SAC database header."
    )


def sac_angle_to_float(
    value: str,
) -> float | None:

    value = clean_string(
        value
    )

    if not value:
        return None

    # Common values are simply degrees.
    number = safe_float(
        value
    )

    if number is not None:
        return number

    return None


def sac_size_to_arcmin(
    value: str,
) -> float | None:
    """
    Parse common SAC angular-size formats.

    Supported examples:

        12.5
        12.5'
        12.5 m
        1.2d
        1.2 deg
        30"
        30 arcsec
        4 x 2
        4.0x2.0

    For compound sizes, the largest dimension is returned.

    SAC's deep-sky database stores sizes primarily as angular dimensions;
    this parser deliberately keeps the largest dimension because the
    application currently expects one diameter.
    """

    text = clean_string(
        value
    ).lower()

    if not text:
        return None

    text = (
        text
        .replace("×", "x")
        .replace("–", "-")
        .replace("—", "-")
    )

    # Extract all numeric tokens.
    numbers = []

    for match in re.finditer(
        r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)",
        text,
    ):

        number = safe_float(
            match.group(0)
        )

        if number is not None:
            numbers.append(
                number
            )

    if not numbers:
        return None

    largest = max(
        numbers
    )

    # Degrees.
    if (
        "deg" in text
        or re.search(
            r"\d\s*d(?:\b|$)",
            text,
        )
    ):
        return (
            largest
            * 60.0
        )

    # Arcseconds.
    if (
        '"' in text
        or "arcsec" in text
        or "second" in text
    ):
        return (
            largest
            / 60.0
        )

    # Otherwise SAC's normal deep-sky size is arcminutes.
    return largest


def parse_sac(
    path: Path,
) -> dict[str, dict]:

    print()
    print(
        "Parsing Saguaro Astronomy Club "
        "Deep Sky Database..."
    )

    raw = path.read_text(
        encoding="latin-1",
        errors="replace",
    )

    lines = raw.splitlines()

    header_index, header = (
        find_sac_header(
            lines
        )
    )

    delimiter = (
        ","
        if "," in lines[
            header_index
        ]
        else "|"
    )

    normalized_header = [
        field.strip(
            ' "\t\r\n'
        )
        for field in header
    ]

    lower_header = [
        field.lower()
        for field in normalized_header
    ]

    def find_column(
        candidates,
    ) -> int | None:

        for candidate in candidates:

            for index, field in enumerate(
                lower_header
            ):

                if field == candidate:
                    return index

        for candidate in candidates:

            for index, field in enumerate(
                lower_header
            ):

                if candidate in field:
                    return index

        return None

    name_index = find_column(
        [
            "name",
            "object",
            "id",
            "object name",
        ]
    )

    size_index = find_column(
        [
            "size",
            "size (arcmin)",
            "diameter",
        ]
    )

    pa_index = find_column(
        [
            "pa",
            "position angle",
            "position_angle",
            "pos angle",
        ]
    )

    if name_index is None:
        raise RuntimeError(
            "Could not identify SAC name column.\n"
            f"Columns: {normalized_header}"
        )

    if size_index is None:
        raise RuntimeError(
            "Could not identify SAC size column.\n"
            f"Columns: {normalized_header}"
        )

    result = {}

    reader = csv.reader(
        lines[
            header_index + 1:
        ],
        delimiter=delimiter,
        quotechar='"',
    )

    for fields in reader:

        if len(fields) <= max(
            name_index,
            size_index,
        ):
            continue

        raw_name = clean_string(
            fields[name_index]
        )

        if not raw_name:
            continue

        normalized = (
            normalize_catalog_name(
                raw_name
            )
        )

        if not normalized:
            continue

        size = sac_size_to_arcmin(
            fields[size_index]
        )

        if size is None:
            continue

        pa = None

        if (
            pa_index is not None
            and len(fields) > pa_index
        ):
            pa = sac_angle_to_float(
                fields[pa_index]
            )

        result[normalized] = {
            "diameter": size,
            "position_angle": pa,
        }

    print(
        f"SAC usable sizes: "
        f"{len(result):,}"
    )

    return result


def apply_sac(
    objects: dict[str, dict],
    sac: dict[str, dict],
) -> int:

    updated = 0

    for obj in objects.values():

        if obj["diameter"] is not None:
            continue

        key = obj[
            "normalized_name"
        ]

        if not key:
            continue

        data = sac.get(
            key
        )

        if data is None:
            continue

        obj["diameter"] = (
            data["diameter"]
        )

        obj["position_angle"] = (
            data["position_angle"]
        )

        obj["diameter_source"] = (
            "SAC"
        )

        obj["diameter_quality"] = (
            "deep_sky_database"
        )

        updated += 1

    return updated


# ============================================================================
# Coordinate fallback matching
# ============================================================================

def apply_coordinate_fallback(
    objects: dict[str, dict],
    external_objects: list[dict],
    source_name: str,
    quality: str,
) -> int:
    """
    Match unresolved objects to an external catalog by sky position.

    This is useful when the catalog's identifier isn't exactly NGCxxxx
    or ICxxxx.

    Only unresolved objects are changed.
    """

    updated = 0

    for obj in objects.values():

        if obj["diameter"] is not None:
            continue

        best = None
        best_distance = float(
            "inf"
        )

        for candidate in external_objects:

            if candidate.get(
                "diameter"
            ) is None:
                continue

            distance = (
                angular_distance_deg(
                    obj["ra"],
                    obj["dec"],
                    candidate["ra"],
                    candidate["dec"],
                )
            )

            if distance < best_distance:

                best = candidate
                best_distance = distance

        if (
            best is None
            or best_distance
            > COORD_MATCH_RADIUS_DEG
        ):
            continue

        obj["diameter"] = (
            best["diameter"]
        )

        obj["minor_diameter"] = (
            best.get(
                "minor_diameter"
            )
        )

        obj["position_angle"] = (
            best.get(
                "position_angle"
            )
        )

        obj["diameter_source"] = (
            source_name
        )

        obj["diameter_quality"] = (
            quality
        )

        updated += 1

    return updated


# ============================================================================
# VizieR NGC 2000.0
# ============================================================================

def parse_ngc2000(
    path: Path,
) -> dict[str, float]:

    print()
    print(
        "Parsing VizieR NGC 2000.0..."
    )

    text = path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    lines = [
        line
        for line in text.splitlines()
        if line.strip()
        and not line.startswith("#")
        and not line.startswith("-")
    ]

    header_index = None

    for i, line in enumerate(
        lines
    ):

        fields = [
            x.strip()
            for x in line.split("\t")
        ]

        lowered = {
            x.lower()
            for x in fields
        }

        if (
            "name" in lowered
            and "size" in lowered
        ):
            header_index = i
            break

    if header_index is None:
        raise RuntimeError(
            "Could not identify NGC2000 "
            "TSV header."
        )

    header = [
        x.strip()
        for x in lines[
            header_index
        ].split("\t")
    ]

    name_index = next(
        i
        for i, x in enumerate(header)
        if x.lower() == "name"
    )

    size_index = next(
        i
        for i, x in enumerate(header)
        if x.lower() == "size"
    )

    result = {}

    for line in lines[
        header_index + 1:
    ]:

        fields = line.split(
            "\t"
        )

        if len(fields) <= max(
            name_index,
            size_index,
        ):
            continue

        name = normalize_catalog_name(
            fields[name_index]
        )

        size = positive_float(
            fields[size_index]
        )

        if (
            name is None
            or size is None
        ):
            continue

        result[name] = size

    print(
        f"NGC2000 usable sizes: "
        f"{len(result):,}"
    )

    return result


def apply_ngc2000(
    objects: dict[str, dict],
    sizes: dict[str, float],
) -> int:

    updated = 0

    for obj in objects.values():

        if obj["diameter"] is not None:
            continue

        key = obj[
            "normalized_name"
        ]

        if not key:
            continue

        size = sizes.get(
            key
        )

        if size is None:
            continue

        obj["diameter"] = size

        obj["diameter_source"] = (
            "NGC2000"
        )

        obj["diameter_quality"] = (
            "largest_dimension"
        )

        updated += 1

    return updated


# ============================================================================
# Messier
# ============================================================================

def build_messier(
    ngc_rows: list[dict[str, str]],
    addendum_rows: list[dict[str, str]],
    enriched_objects: dict[str, dict],
) -> list[dict]:

    messier = {}

    for row in (
        ngc_rows
        + addendum_rows
    ):

        m_field = clean_string(
            row.get("M")
        )

        if not m_field:
            continue

        try:
            number = int(
                float(m_field)
            )
        except ValueError:
            continue

        obj = make_openngc_object(
            row
        )

        if obj is None:
            continue

        # Try to replace this with the enriched NGC/IC version by
        # coordinate.
        best = None
        best_distance = float(
            "inf"
        )

        for candidate in (
            enriched_objects.values()
        ):

            distance = (
                angular_distance_deg(
                    obj["ra"],
                    obj["dec"],
                    candidate["ra"],
                    candidate["dec"],
                )
            )

            if distance < best_distance:
                best = candidate
                best_distance = distance

        if (
            best is not None
            and best_distance
            <= COORD_MATCH_RADIUS_DEG
        ):
            obj["diameter"] = (
                best["diameter"]
            )

            obj["minor_diameter"] = (
                best["minor_diameter"]
            )

            obj["position_angle"] = (
                best["position_angle"]
            )

            obj["diameter_source"] = (
                best["diameter_source"]
            )

            obj["diameter_quality"] = (
                best["diameter_quality"]
            )

            obj["simbad_id"] = (
                best["simbad_id"]
            )

        obj["name"] = (
            f"M{number}"
        )

        # Prefer the first/high-quality NGC.csv-derived object.
        if (
            obj["name"]
            not in messier
        ):
            messier[
                obj["name"]
            ] = obj

    return list(
        messier.values()
    )


# ============================================================================
# Output
# ============================================================================

def format_number(
    value,
) -> str:

    number = positive_float(
        value
    )

    if number is None:
        return "0"

    return f"{number:.3f}"


def application_row(
    obj: dict,
) -> dict:

    return {
        "name": obj["name"],
        "ra": f'{obj["ra"]:.6f}',
        "dec": f'{obj["dec"]:.6f}',
        "diameter": format_number(
            obj["diameter"]
        ),
    }


def write_application_csv(
    path: Path,
    objects: list[dict],
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as fh:

        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "name",
                "ra",
                "dec",
                "diameter",
            ],
        )

        writer.writeheader()

        for obj in objects:
            writer.writerow(
                application_row(
                    obj
                )
            )

    print(
        f"Wrote {len(objects):,} "
        f"objects -> {path}"
    )


def write_master_csv(
    path: Path,
    objects: list[dict],
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    fields = [
        "name",
        "ra",
        "dec",
        "diameter",
        "minor_diameter",
        "position_angle",
        "diameter_source",
        "diameter_quality",
        "object_type",
        "simbad_id",
    ]

    with path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as fh:

        writer = csv.DictWriter(
            fh,
            fieldnames=fields,
        )

        writer.writeheader()

        for obj in objects:

            writer.writerow(
                {
                    "name": obj["name"],
                    "ra": (
                        f'{obj["ra"]:.6f}'
                    ),
                    "dec": (
                        f'{obj["dec"]:.6f}'
                    ),
                    "diameter": (
                        format_number(
                            obj[
                                "diameter"
                            ]
                        )
                    ),
                    "minor_diameter": (
                        format_number(
                            obj[
                                "minor_diameter"
                            ]
                        )
                    ),
                    "position_angle": (
                        format_number(
                            obj[
                                "position_angle"
                            ]
                        )
                    ),
                    "diameter_source": (
                        obj[
                            "diameter_source"
                        ]
                    ),
                    "diameter_quality": (
                        obj[
                            "diameter_quality"
                        ]
                    ),
                    "object_type": (
                        obj[
                            "object_type"
                        ]
                    ),
                    "simbad_id": (
                        obj[
                            "simbad_id"
                        ]
                    ),
                }
            )

    print(
        f"Wrote master catalog -> {path}"
    )


# ============================================================================
# Sorting
# ============================================================================

def sort_key(
    obj: dict,
):

    name = obj["name"].upper()

    match = re.fullmatch(
        r"M(\d+)",
        name,
    )

    if match:
        return (
            0,
            int(match.group(1)),
        )

    match = re.fullmatch(
        r"NGC(\d+)",
        name,
    )

    if match:
        return (
            0,
            int(match.group(1)),
        )

    match = re.fullmatch(
        r"IC(\d+)",
        name,
    )

    if match:
        return (
            0,
            int(match.group(1)),
        )

    return (
        1,
        name,
    )


# ============================================================================
# Statistics
# ============================================================================

def report(
    title: str,
    objects: list[dict],
) -> None:

    total = len(
        objects
    )

    sized = [
        obj
        for obj in objects
        if obj["diameter"] is not None
        and obj["diameter"] > 0
    ]

    print()
    print(
        f"=== {title.upper()} ==="
    )

    print(
        f"Total Objects:       "
        f"{total:,}"
    )

    print(
        f"Valid Size Entries:  "
        f"{len(sized):,}"
    )

    print(
        f"Missing:             "
        f"{total - len(sized):,}"
    )

    if sized:

        values = [
            obj["diameter"]
            for obj in sized
        ]

        print(
            f"Min Diameter:        "
            f"{min(values):.4f}"
        )

        print(
            f"Max Diameter:        "
            f"{max(values):.4f}"
        )

        print(
            f"Avg Diameter:        "
            f"{sum(values) / len(values):.4f}"
        )

    sources = {}

    for obj in objects:

        source = (
            obj["diameter_source"]
            or "MISSING"
        )

        sources[source] = (
            sources.get(
                source,
                0,
            )
            + 1
        )

    print()
    print(
        "Size source:"
    )

    for source, count in sorted(
        sources.items(),
        key=lambda item: (
            -item[1],
            item[0],
        ),
    ):

        print(
            f"  {source:16s} "
            f"{count:,}"
        )


# ============================================================================
# Sharpless HII Regions catalog (Sh2) -- maximum angular diameters
#
# Source: Sharpless S. 1959, ApJS 4, 257
# VizieR catalog VII/20 -- 313 galactic HII regions
#
# Sh2 diameters are the "maximum angular diameter" of the HII region,
# which captures the full extent -- much better for large nebulae than
# the LBN-based sizes OpenNGC uses (which measure only the brightest core).
# ============================================================================

def download_sharpless() -> dict[int, float]:
    """Download Sharpless catalog from VizieR.
    Returns dict mapping Sh2-number -> diameter in arcminutes."""
    print()
    print("Downloading Sharpless HII Regions catalog (VII/20)...")

    text = requests.get(
        SHARPLESS_URL,
        timeout=REQUEST_TIMEOUT,
        headers={"User-Agent": "LocalAnnotate-DSO-Catalog/1.0"},
    ).text

    # Write raw for reproducibility
    SHARPLESS_RAW.parent.mkdir(parents=True, exist_ok=True)
    SHARPLESS_RAW.write_text(text, encoding="utf-8")

    result: dict[int, float] = {}
    in_data = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        if line.startswith("Sh2") or line.startswith("----"):
            in_data = True
            continue
        if not in_data:
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                sh2_num = int(parts[0])
                diam = float(parts[1])
                if diam > 0:
                    result[sh2_num] = diam
            except (ValueError, IndexError):
                continue

    print(f"Sharpless: {len(result)} HII regions with diameters")
    return result


def build_sh2_crossref() -> dict[str, float]:
    """Cross-reference Sh2 objects to NGC/IC via SIMBAD identifiers.

    Downloads the Sharpless catalog and queries SIMBAD for each Sh2
    object to find NGC/IC designations. Results are cached in
    sh2_ngc_crossref.csv so SIMBAD is only queried once.

    Returns dict mapping 'NGCxxxx'/'ICxxxx' -> diameter in arcminutes.
    """
    crossref_path = SH2_NGC_CROSSREF

    # Return cached cross-reference if it exists
    if crossref_path.exists():
        print("Loading cached Sh2→NGC/IC cross-reference...")
        result: dict[str, float] = {}
        with crossref_path.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                name = (row.get("name") or "").strip().upper()
                try:
                    diam = float(row.get("diameter_arcmin", ""))
                except (ValueError, TypeError):
                    continue
                if name and diam > 0:
                    result[name] = diam
        print(f"  {len(result)} cross-referenced Sh2 objects")
        return result

    # Download Sharpless catalog
    sh2_data = download_sharpless()
    if not sh2_data:
        return {}

    # Query SIMBAD for NGC/IC identifiers
    print("Cross-referencing Sh2 → NGC/IC via SIMBAD...")
    try:
        from astroquery.simbad import Simbad
    except ImportError:
        print("  astroquery not installed -- skipping SIMBAD cross-reference.")
        print("  Run with venv Python to generate the cross-reference file once.")
        return {}

    import re
    import time as time_mod

    s = Simbad(timeout=300)
    s.ROW_LIMIT = -1
    s.add_votable_fields("ids")

    sh2_names = [f"Sh2-{n}" for n in sorted(sh2_data.keys())]
    result: dict[str, float] = {}

    for batch_start in range(0, len(sh2_names), SIMBAD_BATCH_SIZE):
        batch = sh2_names[batch_start:batch_start + SIMBAD_BATCH_SIZE]
        try:
            table = s.query_objects(batch, async_job=True)
            if table:
                for row in table:
                    ids = str(row.get("ids", "")).strip()
                    sh2_user = str(row["user_specified_id"]).strip()
                    sh2_num_match = re.search(r"Sh2-0*(\d+)", sh2_user, re.IGNORECASE)
                    if not sh2_num_match:
                        continue
                    sh2_num = int(sh2_num_match.group(1))
                    diam = sh2_data.get(sh2_num)
                    if diam is None:
                        continue
                    # Extract NGC/IC identifiers (only real catalog numbers: 1-4 digits)
                    for m in re.finditer(r"\bNGC\s*0*(\d{1,4})\b", ids, re.IGNORECASE):
                        key = f"NGC{int(m.group(1))}"
                        if key not in result:
                            result[key] = diam
                    for m in re.finditer(r"\bIC\s*0*(\d{1,4})\b", ids, re.IGNORECASE):
                        key = f"IC{int(m.group(1))}"
                        if key not in result:
                            result[key] = diam
        except Exception as exc:
            print(f"  SIMBAD batch error: {exc}")
        time_mod.sleep(SIMBAD_BATCH_DELAY)

    print(f"  {len(result)} NGC/IC objects cross-referenced to Sh2")

    # Cache for next run
    crossref_path.parent.mkdir(parents=True, exist_ok=True)
    with crossref_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["name", "diameter_arcmin"])
        writer.writeheader()
        for name in sorted(result.keys()):
            writer.writerow({"name": name, "diameter_arcmin": str(result[name])})
    print(f"  Cached to {crossref_path}")

    return result


def apply_sh2_sizes(
    objects: dict[str, dict],
    sh2_sizes: dict[str, float],
) -> int:
    """Apply Sharpless HII region diameters to matching NGC/IC objects.

    Only overrides when Sh2 size is larger (Sh2 measures max extent).
    """
    updated = 0
    for obj in objects.values():
        key = obj.get("normalized_name")
        if not key:
            continue
        new_diameter = sh2_sizes.get(key)
        if new_diameter is None:
            continue
        old_diameter = obj["diameter"]
        if old_diameter is not None and old_diameter >= new_diameter:
            continue
        obj["diameter"] = new_diameter
        obj["diameter_source"] = "Sharpless"
        obj["diameter_quality"] = "hii_max_diameter"
        updated += 1
    return updated


# ============================================================================
# Large-DSO size overrides (curated, applied last so they always win)
# ============================================================================

LARGE_DSO_OVERRIDES_CSV = DATA_DIR / "large_dso_overrides.csv"


def load_large_dso_overrides() -> dict[str, float]:
    """Load manually-curated angular sizes for well-known large nebulae.

    The CSV must have columns: name, diameter_arcmin, note
    (note is ignored at runtime but helps humans who read the file).
    """
    path = LARGE_DSO_OVERRIDES_CSV
    if not path.exists():
        print(f"  (no override file at {path} -- skipping)")
        return {}

    overrides: dict[str, float] = {}
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            name = (row.get("name") or "").strip().upper()
            if not name:
                continue
            try:
                diameter = float(row.get("diameter_arcmin", ""))
            except (ValueError, TypeError):
                continue
            if diameter > 0:
                overrides[name] = diameter

    print(f"\nLoaded {len(overrides):,} large-DSO size overrides from {path}")
    return overrides


def apply_large_dso_overrides(
    objects: dict[str, dict],
    overrides: dict[str, float],
) -> int:
    """Apply curated large-object sizes, overriding whatever earlier
    catalogs provided. Returns count of objects updated."""
    updated = 0

    for obj in objects.values():
        key = obj.get("normalized_name")
        if not key:
            continue
        new_diameter = overrides.get(key)
        if new_diameter is None:
            continue

        old_diameter = obj["diameter"]
        if old_diameter is not None and old_diameter >= new_diameter:
            continue  # existing size is already as big or bigger

        obj["diameter"] = new_diameter
        obj["diameter_source"] = "curated_override"
        obj["diameter_quality"] = "astrophotography_consensus"
        updated += 1

    return updated


# ============================================================================
# Main
# ============================================================================

def main():

    print(
        "=" * 78
    )

    print(
        "DSO CATALOG GENERATOR"
    )

    print(
        "OpenNGC -> SIMBAD -> SAC -> NGC2000"
    )

    print(
        "=" * 78
    )

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    RAW_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # ------------------------------------------------------------------
    # Download OpenNGC.
    # ------------------------------------------------------------------

    download_file(
        NGC_URL,
        NGC_RAW,
    )

    download_file(
        ADDENDUM_URL,
        ADDENDUM_RAW,
    )

    # ------------------------------------------------------------------
    # Load OpenNGC.
    # ------------------------------------------------------------------

    ngc_rows = load_openngc(
        NGC_RAW
    )

    addendum_rows = load_openngc(
        ADDENDUM_RAW
    )

    print()
    print(
        f"OpenNGC NGC rows: "
        f"{len(ngc_rows):,}"
    )

    print(
        f"OpenNGC addendum rows: "
        f"{len(addendum_rows):,}"
    )

    # ------------------------------------------------------------------
    # Build master NGC/IC objects.
    # ------------------------------------------------------------------

    objects = build_openngc_objects(
        ngc_rows
    )

    # ------------------------------------------------------------------
    # Initial OpenNGC statistics.
    # ------------------------------------------------------------------

    initial_sized = sum(
        1
        for obj in objects.values()
        if obj["diameter"] is not None
    )

    missing = [
        obj
        for obj in objects.values()
        if obj["diameter"] is None
    ]

    print()
    print(
        "Initial OpenNGC coverage:"
    )

    print(
        f"  objects: "
        f"{len(objects):,}"
    )

    print(
        f"  sized: "
        f"{initial_sized:,}"
    )

    print(
        f"  missing: "
        f"{len(missing):,}"
    )

    # ------------------------------------------------------------------
    # SIMBAD.
    #
    # ONLY unresolved NGC/IC objects are queried.
    # ------------------------------------------------------------------

    simbad_names = [
        obj["name"]
        for obj in missing
        if (
            obj["name"].startswith("NGC")
            or obj["name"].startswith("IC")
        )
    ]

    simbad_results = query_simbad(
        simbad_names
    )

    simbad_count = apply_simbad(
        objects,
        simbad_results,
    )

    print()
    print(
        f"SIMBAD applied: "
        f"{simbad_count:,}"
    )

    # ------------------------------------------------------------------
    # SAC.
    # ------------------------------------------------------------------

    still_missing = [
        obj
        for obj in objects.values()
        if obj["diameter"] is None
    ]

    if still_missing:

        download_file(
            SAC_URL,
            SAC_RAW,
        )

        sac_file = extract_sac_archive()

        sac_sizes = parse_sac(
            sac_file
        )

        sac_count = apply_sac(
            objects,
            sac_sizes,
        )

        print()
        print(
            f"SAC applied: "
            f"{sac_count:,}"
        )

    # ------------------------------------------------------------------
    # NGC 2000.0.
    # ------------------------------------------------------------------

    still_missing = [
        obj
        for obj in objects.values()
        if obj["diameter"] is None
    ]

    if still_missing:

        download_file(
            VIZIER_NGC2000_URL,
            NGC2000_RAW,
        )

        ngc2000_sizes = (
            parse_ngc2000(
                NGC2000_RAW
            )
        )

        ngc2000_count = (
            apply_ngc2000(
                objects,
                ngc2000_sizes,
            )
        )

        print()
        print(
            f"NGC2000 applied: "
            f"{ngc2000_count:,}"
        )

    # ------------------------------------------------------------------
    # Sharpless HII Regions (Sh2) -- max angular diameters.
    # ------------------------------------------------------------------

    sh2_sizes = build_sh2_crossref()
    if sh2_sizes:
        sh2_count = apply_sh2_sizes(objects, sh2_sizes)
        print(f"\nSharpless Sh2 applied: {sh2_count:,}")

    # ------------------------------------------------------------------
    # Large-DSO size overrides (curated, always wins).
    # ------------------------------------------------------------------

    overrides = load_large_dso_overrides()
    if overrides:
        override_count = apply_large_dso_overrides(objects, overrides)
        print(f"Large-DSO overrides applied: {override_count:,}")

    # ------------------------------------------------------------------
    # Final NGC/IC lists.
    # ------------------------------------------------------------------

    ngc_objects = [
        obj
        for obj in objects.values()
        if obj["name"].startswith(
            "NGC"
        )
    ]

    ic_objects = [
        obj
        for obj in objects.values()
        if obj["name"].startswith(
            "IC"
        )
    ]

    ngc_objects.sort(
        key=sort_key
    )

    ic_objects.sort(
        key=sort_key
    )

    master_objects = (
        ngc_objects
        + ic_objects
    )

    # ------------------------------------------------------------------
    # Messier.
    # ------------------------------------------------------------------

    messier_objects = build_messier(
        ngc_rows,
        addendum_rows,
        objects,
    )

    messier_objects.sort(
        key=sort_key
    )

    # ------------------------------------------------------------------
    # Write files.
    # ------------------------------------------------------------------

    write_master_csv(
        MASTER_CSV,
        master_objects,
    )

    write_application_csv(
        MESSIER_CSV,
        messier_objects,
    )

    write_application_csv(
        NGC_CSV,
        ngc_objects,
    )

    write_application_csv(
        IC_CSV,
        ic_objects,
    )

    # ------------------------------------------------------------------
    # Reports.
    # ------------------------------------------------------------------

    report(
        "MESSIER",
        messier_objects,
    )

    report(
        "NGC",
        ngc_objects,
    )

    report(
        "IC",
        ic_objects,
    )

    # ------------------------------------------------------------------
    # Overall report.
    # ------------------------------------------------------------------

    total = len(
        master_objects
    )

    sized = sum(
        1
        for obj in master_objects
        if obj["diameter"] is not None
    )

    missing = (
        total - sized
    )

    print()
    print(
        "=" * 78
    )

    print(
        "FINAL COVERAGE"
    )

    print(
        "=" * 78
    )

    print(
        f"NGC + IC objects:       "
        f"{total:,}"
    )

    print(
        f"Objects with size:      "
        f"{sized:,}"
    )

    print(
        f"Objects still missing:  "
        f"{missing:,}"
    )

    if total:
        print(
            f"Coverage:               "
            f"{100.0 * sized / total:.2f}%"
        )

    print()
    print(
        "Generated:"
    )

    print(
        f"  {MESSIER_CSV}"
    )

    print(
        f"  {NGC_CSV}"
    )

    print(
        f"  {IC_CSV}"
    )

    print(
        f"  {MASTER_CSV}"
    )

    print()
    print(
        "Done."
    )


# ============================================================================
# Entry point
# ============================================================================

if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print()
        print(
            "Interrupted."
        )

        sys.exit(130)

    except Exception as exc:

        print()
        print(
            "ERROR:"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

        sys.exit(1)

