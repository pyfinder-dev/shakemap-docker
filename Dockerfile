FROM python:3.12-slim

# ---------- Environment ----------
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    RUNTIME_ROOT=/home/sysop/runtime \
    SERVICE_ROOT=/home/sysop/runtime/shakemap \
    SHAKEMAP_PROFILE=default \
    SHAKEMAP_PORT=9010 \
    SHAKEMAP_REQUIRE_MOUNT=0 \
    SHAKEMAP_MODULES="select assemble model contour mapping stations gridxml"

# ---------- System packages ----------
# - git: to clone the official ShakeMap repo
# - curl: required by configure-shakemap.sh for data downloads
# - nano: simple text editor inside container
# - build / numeric libs: likely needed by ShakeMap deps
# - gdal-bin, libgdal-dev: required by Fiona (and thus shakemap-modules)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
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
COPY scripts /app/scripts

RUN chmod +x /app/entrypoint.sh /app/scripts/*.sh

# ---------- Create non-root user and fix ownership ----------
RUN groupadd -g 1000 sysop && useradd -u 1000 -g 1000 -ms /bin/bash sysop \
 && mkdir -p "${RUNTIME_ROOT}" \
 && chown -R sysop:sysop "${RUNTIME_ROOT}" /app /opt/shakemap

USER sysop

# ---------- Networking ----------
EXPOSE 9010

ENTRYPOINT ["/app/entrypoint.sh"]