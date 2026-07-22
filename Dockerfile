FROM python:3.12-slim

# ---------- Immutable build inputs ----------
ARG SHAKEMAP_SOURCE_URL
ARG SHAKEMAP_RELEASE_TAG
ARG SHAKEMAP_RELEASE_VERSION
ARG SHAKEMAP_SOURCE_COMMIT
ARG SERVICE_SOURCE_COMMIT=unavailable
ARG SERVICE_WORKTREE_DIRTY=unknown
ARG BUILD_TIMESTAMP_UTC

LABEL org.opencontainers.image.created="${BUILD_TIMESTAMP_UTC}" \
      org.opencontainers.image.revision="${SERVICE_SOURCE_COMMIT}" \
      org.usgs.shakemap.source="${SHAKEMAP_SOURCE_URL}" \
      org.usgs.shakemap.release="${SHAKEMAP_RELEASE_TAG}" \
      org.usgs.shakemap.version="${SHAKEMAP_RELEASE_VERSION}" \
      org.usgs.shakemap.commit="${SHAKEMAP_SOURCE_COMMIT}" \
      org.usgs.shakemap.service.worktree-dirty="${SERVICE_WORKTREE_DIRTY}" \
      org.usgs.shakemap.identity-file="/opt/shakemap-build/identity.json" \
      org.usgs.shakemap.dependency-inventory="/opt/shakemap-build/dependencies.txt"

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

# ---------- Fetch and install the resolved ShakeMap release ----------
# The host resolver supplies one official stable tag and its full commit.
# Fetch the tag, detach at the requested commit, and fail closed on mismatch.
RUN test -n "${SHAKEMAP_SOURCE_URL}" \
 && test -n "${SHAKEMAP_RELEASE_TAG}" \
 && test -n "${SHAKEMAP_SOURCE_COMMIT}" \
 && mkdir -p /opt/shakemap \
 && git -C /opt/shakemap init \
 && git -C /opt/shakemap remote add origin "${SHAKEMAP_SOURCE_URL}" \
 && git -C /opt/shakemap fetch --depth 1 origin \
      "refs/tags/${SHAKEMAP_RELEASE_TAG}:refs/tags/${SHAKEMAP_RELEASE_TAG}" \
 && git -C /opt/shakemap checkout --detach "${SHAKEMAP_SOURCE_COMMIT}" \
 && test "$(git -C /opt/shakemap rev-parse HEAD)" = "${SHAKEMAP_SOURCE_COMMIT}" \
 && test "$(git -C /opt/shakemap rev-parse HEAD^{commit})" = "${SHAKEMAP_SOURCE_COMMIT}"

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

# ---------- Record installed identity and complete dependency inventory ----------
RUN mkdir -p /opt/shakemap-build \
 && python -m pip freeze --all | LC_ALL=C sort > /opt/shakemap-build/dependencies.txt \
 && python -m shakemap_service.build_identity write \
      --output /opt/shakemap-build/identity.json \
      --dependencies /opt/shakemap-build/dependencies.txt \
      --source-url "${SHAKEMAP_SOURCE_URL}" \
      --release-tag "${SHAKEMAP_RELEASE_TAG}" \
      --release-version "${SHAKEMAP_RELEASE_VERSION}" \
      --source-commit "${SHAKEMAP_SOURCE_COMMIT}" \
      --service-commit "${SERVICE_SOURCE_COMMIT}" \
      --service-worktree-dirty "${SERVICE_WORKTREE_DIRTY}" \
      --build-timestamp-utc "${BUILD_TIMESTAMP_UTC}" \
 && chmod 0444 /opt/shakemap-build/identity.json /opt/shakemap-build/dependencies.txt

# ---------- Create non-root user and fix ownership ----------
RUN groupadd -g 1000 sysop && useradd -u 1000 -g 1000 -ms /bin/bash sysop \
 && mkdir -p "${RUNTIME_ROOT}" \
 && chown -R sysop:sysop "${RUNTIME_ROOT}" /app /opt/shakemap

USER sysop

# ---------- Networking ----------
EXPOSE 9010

ENTRYPOINT ["/app/entrypoint.sh"]
