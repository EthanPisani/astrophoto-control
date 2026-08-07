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
# wget, unzip: for downloading/extracting ASTAP
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# -- Install ASTAP --------------------------------------------------
# Download the official amd64 .deb, extract astap_cli from it.
# The .deb is the primary distribution and always has the latest CLI.
RUN wget -q "https://sourceforge.net/projects/astap-program/files/linux_installer/astap_amd64.deb/download" \
         -O /tmp/astap_amd64.deb && \
    dpkg-deb -x /tmp/astap_amd64.deb /tmp/astap_extract && \
    cp /tmp/astap_extract/opt/astap/astap_cli /usr/local/bin/ && \
    chmod +x /usr/local/bin/astap_cli && \
    rm -rf /tmp/astap_amd64.deb /tmp/astap_extract

# D50 star database (~200 MB)
# SourceForge /download links redirect; --content-disposition ensures
# we get the real file, not an HTML mirror-select page.
RUN wget -q --content-disposition \
         "https://sourceforge.net/projects/astap-program/files/star_databases/d50_star_database.deb/download" \
         -O /tmp/d50.deb && \
    dpkg-deb -x /tmp/d50.deb /tmp/d50_extract && \
    find /tmp/d50_extract -name '*.290' -exec cp {} /opt/astap/ \; && \
    rm -rf /tmp/d50.deb /tmp/d50_extract

# G05 wider-field blind-solve database
RUN wget -q --content-disposition \
         "https://sourceforge.net/projects/astap-program/files/star_databases/g05_star_database.deb/download" \
         -O /tmp/g05.deb && \
    dpkg-deb -x /tmp/g05.deb /tmp/g05_extract && \
    find /tmp/g05_extract -name '*.290' -exec cp {} /opt/astap/ \; && \
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
COPY --from=builder /usr/local/bin/astap_cli /usr/local/bin/astap_cli
COPY --from=builder /opt/astap /opt/astap

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

EXPOSE 7777
VOLUME ["/data"]

# Health check: ensure Flask is responding
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:7777/health/api')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
