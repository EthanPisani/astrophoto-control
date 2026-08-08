#!/usr/bin/env bash
# ============================================================================
# AstroCap Docker entrypoint
# ============================================================================
# On first launch, ensures catalog directories exist.  The catalogs
# themselves are baked into the image at /root/.local_annotate/ during
# the Docker build, so no network access is needed at runtime.
#
# If catalogs are missing (e.g. because you built without the catalog
# stage), this will attempt to build them — but that requires internet.
# ============================================================================

set -euo pipefail

CATALOG_HOME="${HOME:-/root}/.local_annotate"
DSO_DIR="${CATALOG_HOME}/dso-catalogs"
STAR_DIR="${CATALOG_HOME}/star-catalogs"
ASTAP_DB_DIR="/opt/astap"

echo "=== AstroCap starting ==="
echo "  ASTROCAP_PORT=${ASTROCAP_PORT:-7777}"
echo "  ASTROCAP_OUTDIR=${ASTROCAP_OUTDIR:-/data}"
echo "  ASTROCAP_LOCAL_OUTDIR=${ASTROCAP_LOCAL_OUTDIR:-/data_local}"
echo "  ASTROCAP_DB_PATH=${ASTROCAP_DB_PATH:-/data/astrocap.db}"
echo "  GPHOTO_API_BASE=${GPHOTO_API_BASE:-http://10.0.0.69:8080}"
echo "  ASTROCAP_SOLVER_BACKEND=${ASTROCAP_SOLVER_BACKEND:-local}"
echo "  ASTROCAP_MAX_RAM_GB=${ASTROCAP_MAX_RAM_GB:-4}"

# -- Ensure output directories exist --------------------------------
mkdir -p "${ASTROCAP_OUTDIR:-/data}"
mkdir -p "${ASTROCAP_LOCAL_OUTDIR:-/data_local}"
mkdir -p "${ASTROCAP_OUTDIR:-/data}/sessions"
mkdir -p "${ASTROCAP_OUTDIR:-/data}/calibration"
mkdir -p "${ASTROCAP_OUTDIR:-/data}/platesolve"
mkdir -p "${ASTROCAP_OUTDIR:-/data}/platesolve_uploads"

# -- Verify ASTAP is functional -------------------------------------
if command -v astap_cli &>/dev/null; then
    echo "  astap_cli: $(command -v astap_cli)"
    if ls "${ASTAP_DB_DIR}"/*.290 &>/dev/null 2>&1; then
        db_count=$(ls "${ASTAP_DB_DIR}"/*.290 2>/dev/null | wc -l)
        echo "  ASTAP star databases: ${db_count} files in ${ASTAP_DB_DIR}"
    else
        echo "  WARNING: No ASTAP star databases (*.290) found in ${ASTAP_DB_DIR}"
        echo "  Plate solving with --local backend will fail without these."
    fi
else
    echo "  WARNING: astap_cli not found — --local solver backend will not work"
fi

# -- Verify catalogs ------------------------------------------------
need_build=""
if [ ! -f "${DSO_DIR}/messier.csv" ] || [ ! -f "${DSO_DIR}/ngc.csv" ]; then
    need_build="yes"
fi
if [ ! -f "${STAR_DIR}/known_stars.csv" ] || [ ! -f "${STAR_DIR}/constellation_lines.csv" ]; then
    need_build="yes"
fi

if [ -n "$need_build" ]; then
    echo ""
    echo "=== Catalog files missing, attempting build (needs internet) ==="
    # Order matters: constellation_lines.csv before known_stars.csv
    if python /app/build_constellation_lines.py && \
       python /app/build_dso_catalogs.py && \
       python /app/build_star_catalogs.py; then
        echo "  Catalogs built successfully."
    else
        echo "  WARNING: Catalog build failed. DSO annotation will be limited."
        echo "  You can retry by deleting the catalog dirs and restarting."
    fi
else
    echo "  Catalogs: OK (${DSO_DIR}, ${STAR_DIR})"
fi

echo ""
echo "=== Launching AstroCap on 0.0.0.0:${ASTROCAP_PORT:-7777} ==="

# gthread worker class gives good throughput for the I/O-bound camera
# bridge calls (HTTP to nikon_bulb_server) while keeping memory low.
exec gunicorn \
    --bind "0.0.0.0:${ASTROCAP_PORT:-7777}" \
    --workers "${GUNICORN_WORKERS:-2}" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout "${GUNICORN_TIMEOUT:-120}" \
    --worker-class gthread \
    --access-logfile - \
    --error-logfile - \
    --capture-output \
    app:app
