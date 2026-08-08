# ============================================================================
# AstroCap Docker image
# ============================================================================
#
# Build:
#   docker build -t astrocap .
#
# Run (interactive, with host networking for camera bridge access):
#   docker run --rm -it --network host \
#       -v astrocap_data:/data \
#       -e ASTROCAP_OUTDIR=/data \
#       astrocap
#
# Or use docker-compose:
#   docker compose up -d
#
# The image is fully self-contained after build: star catalogs, DSO
# catalogs, and the ASTAP D50 database are all baked in.  No internet
# access is required at runtime — only a reachable nikon_bulb_server
# instance on the network.

# ------------------------------------------------------------------
# Stage 1: system dependencies + ASTAP + catalog build
# ------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS builder

# -- Install system packages ----------------------------------------
# xz-utils: needed by dpkg-deb -x to decompress .tar.xz inside .deb
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    curl \
    ca-certificates \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# -- Install ASTAP --------------------------------------------------
# Download the official amd64 .deb, extract astap_cli from it.
RUN curl -L --retry 3 --connect-timeout 30 \
         -o /tmp/astap_amd64.deb \
         "https://sourceforge.net/projects/astap-program/files/linux_installer/astap_amd64.deb/download" && \
    dpkg-deb -x /tmp/astap_amd64.deb /tmp/astap_extract && \
    cp /tmp/astap_extract/opt/astap/astap_cli /usr/local/bin/ && \
    chmod +x /usr/local/bin/astap_cli && \
    rm -rf /tmp/astap_amd64.deb /tmp/astap_extract

# D50 star database (~826 MB .deb, extracts to opt/astap/*.1476)
# astap_cli searches: own dir → /opt/astap → /usr/share/astap/data.
# curl -L follows the SourceForge mirror redirect chain properly.
RUN mkdir -p /usr/share/astap/data && \
    curl -L --retry 5 --retry-connrefused --connect-timeout 30 --max-time 600 \
         -o /tmp/d50.deb \
         "https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.deb/download" && \
    dpkg-deb -x /tmp/d50.deb /tmp/d50_extract && \
    cp /tmp/d50_extract/opt/astap/*.1476 /usr/share/astap/data/ 2>/dev/null; \
    cp /tmp/d50_extract/opt/astap/*.290 /usr/share/astap/data/ 2>/dev/null; \
    rm -rf /tmp/d50.deb /tmp/d50_extract && \
    ls /usr/share/astap/data/*.1476 /usr/share/astap/data/*.290 >/dev/null 2>&1 \
    || (echo "FATAL: No star database files extracted from D50 .deb" >&2; exit 1)

# G05 wider-field blind-solve database
RUN curl -L --retry 5 --retry-connrefused --connect-timeout 30 --max-time 600 \
         -o /tmp/g05.deb \
         "https://sourceforge.net/projects/astap-program/files/star_databases/g05_star_database.deb/download" && \
    dpkg-deb -x /tmp/g05.deb /tmp/g05_extract && \
    cp /tmp/g05_extract/opt/astap/*.1476 /usr/share/astap/data/ 2>/dev/null; \
    cp /tmp/g05_extract/opt/astap/*.290 /usr/share/astap/data/ 2>/dev/null; \
    rm -rf /tmp/g05.deb /tmp/g05_extract

# -- Python dependencies --------------------------------------------
COPY requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir -r /build/requirements.txt

# -- Build astronomical catalogs (requires internet) ----------------
# Order matters: constellation_lines.csv must exist before
# build_star_catalogs.py runs (it cross-references HIP numbers).
COPY build_constellation_lines.py /build/
COPY build_dso_catalogs.py /build/
COPY build_star_catalogs.py /build/
# Hand-curated angular-size overrides for well-known large nebulae
COPY large_dso_overrides.csv /build/

# Create target directories
RUN mkdir -p /root/.local_annotate/dso-catalogs && \
    mkdir -p /root/.local_annotate/star-catalogs && \
    cp /build/large_dso_overrides.csv /root/.local_annotate/dso-catalogs/

# Build constellation_lines.csv from Stellarium + HYG (fully online, reproducible)
RUN cd /build && python build_constellation_lines.py

# Build DSO catalogs (Messier/NGC/IC) from OpenNGC, SIMBAD, VizieR
RUN cd /build && python build_dso_catalogs.py

# Build known_stars.csv from HYG + constellation_lines.csv
RUN cd /build && python build_star_catalogs.py

# ------------------------------------------------------------------
# Stage 2: minimal runtime image
# ------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# System libs that rawpy, OpenCV, and astap_cli need at runtime.
# rawpy bundles libraw statically in its wheel -- no system libraw needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgtk-3-0 \
    libglib2.0-0 \
    libgomp1 \
    libgl1-mesa-glx \
    libxrender1 \
    libxext6 \
    libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# -- Copy ASTAP binary + star databases from builder ----------------
# astap_cli is included in the /usr/local/bin directory copy below.
# Databases live at /usr/share/astap/data; /opt/astap is a symlink
# so both search paths astap_cli checks resolve to the same files.
COPY --from=builder /usr/share/astap/data /usr/share/astap/data
RUN ln -s /usr/share/astap/data /opt/astap

# -- Copy Python site-packages from builder -------------------------
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# -- Copy catalogs built in stage 1 ---------------------------------
COPY --from=builder /root/.local_annotate /root/.local_annotate

# -- Copy application code -------------------------------------------
WORKDIR /app
COPY *.py /app/
COPY requirements.txt /app/
COPY templates/ /app/templates/
COPY static/ /app/static/
COPY gunicorn.conf.py /app/
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# -- Runtime configuration -------------------------------------------
ENV ASTROCAP_PORT=7777
ENV ASTROCAP_OUTDIR=/data
ENV ASTROCAP_DB_PATH=/data/astrocap.db
ENV ASTROCAP_MAX_RAM_GB=4
# Default camera bridge location -- override with your Pi's IP
ENV GPHOTO_API_BASE=http://10.0.0.69:8080
# Use the fully-offline ASTAP backend by default
ENV ASTROCAP_SOLVER_BACKEND=local
# Gunicorn settings (override for your hardware)
ENV GUNICORN_WORKERS=2
ENV GUNICORN_THREADS=4
ENV GUNICORN_TIMEOUT=120

EXPOSE 7777
VOLUME ["/data"]

# Health check: gunicorn returns 200 from the /health/api endpoint
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7777/health/api')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
