FROM python:3.12-slim

# ---------- Environment ----------
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    SHAKEMAP_DATA_ROOT=/data/shakemap \
    SHAKEMAP_PROFILE=default \
    SHAKEMAP_PORT=9010 \
    SHAKEMAP_REQUIRE_MOUNT=0

# ---------- System packages ----------
# - git: to clone the official ShakeMap repo
# - nano: simple text editor inside container
# - build / numeric libs: likely needed by ShakeMap deps
# - gdal-bin, libgdal-dev: required by Fiona (and thus shakemap-modules)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    nano \
    gcc \
    g++ \
    gfortran \
    libproj-dev \
    libgeos-dev \
    libopenblas-dev \
    gdal-bin \
    libgdal-dev \
 && rm -rf /var/lib/apt/lists/*

# ---------- Clone and install ShakeMap ----------
# Official main repo lives on USGS GitLab:
#   https://code.usgs.gov/ghsc/esi/shakemap
RUN git clone --depth 1 https://code.usgs.gov/ghsc/esi/shakemap.git /opt/shakemap

# Install ShakeMap and all its Python dependencies as defined by the project itself.
# Even though the docs recommend conda, we rely on pip here inside Docker.
RUN pip install --no-cache-dir /opt/shakemap

# ---------- Install service dependencies (FastAPI HTTP wrapper) ----------
# No local requirements.txt; everything is declared here.
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    python-multipart

# ---------- Create app directory and non-root user ----------
WORKDIR /app

# ---------- Copy service code ----------
COPY shakemap_service /app/shakemap_service
COPY entrypoint.sh /app/entrypoint.sh

RUN chmod +x /app/entrypoint.sh

# ---------- Create non-root user and fix ownership ----------
RUN useradd -ms /bin/bash shakemap \
 && mkdir -p "${SHAKEMAP_DATA_ROOT}" \
 && chown -R shakemap:shakemap "${SHAKEMAP_DATA_ROOT}" /app /opt/shakemap

USER shakemap

# ---------- Networking ----------
EXPOSE 9010

ENTRYPOINT ["/app/entrypoint.sh"]