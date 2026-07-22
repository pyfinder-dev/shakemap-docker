FROM python:3.12-slim

# ---------- Immutable build inputs ----------
ARG SHAKEMAP_SOURCE_URL
ARG SHAKEMAP_RELEASE_TAG
ARG SHAKEMAP_RELEASE_VERSION
ARG SHAKEMAP_SOURCE_COMMIT
ARG SERVICE_SOURCE_COMMIT=unavailable
ARG SERVICE_WORKTREE_DIRTY=unknown
ARG BUILD_TIMESTAMP_UTC

# ---------- Environment ----------
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    RUNTIME_ROOT=/home/sysop/runtime \
    SERVICE_ROOT=/home/sysop/runtime/shakemap \
    SHAKEMAP_PROFILE=default \
    SHAKEMAP_PORT=9010 \
    SHAKEMAP_REQUIRE_MOUNT=0 \
    SHAKEMAP_MODULES="select assemble model contour mapping stations gridxml" \
    CARTOPY_DATA_DIR=/opt/shakemap-support/cartopy \
    SHAKEMAP_STREC_DB=/opt/shakemap-support/strec/moment_tensors.db

# ---------- System packages ----------
# - git: to clone the official ShakeMap repo
# - build / numeric libs: likely needed by ShakeMap deps
# - gdal-bin, libgdal-dev: required by Fiona (and thus shakemap-modules)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
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

# ---------- Immutable generic mapping and STREC support ----------
# Natural Earth is generic mapping support, not event/scenario data. The
# installer verifies every file against an immutable commit and SHA-256.
COPY image-support/natural-earth-v5.1.2.json /opt/shakemap-support/natural-earth-v5.1.2.json
COPY scripts/install-image-support.py /tmp/install-image-support.py
RUN python /tmp/install-image-support.py \
      --manifest /opt/shakemap-support/natural-earth-v5.1.2.json \
      --destination /opt/shakemap-support/cartopy \
 && rm /tmp/install-image-support.py \
 && mkdir -p /opt/shakemap-support/strec \
 && python -c "import importlib.metadata,pathlib; d=importlib.metadata.distribution('usgs-strec'); p=next(d.locate_file(f) for f in d.files if str(f).endswith('strec/data/moment_tensors.db')); pathlib.Path('/opt/shakemap-support/strec/moment_tensors.db').symlink_to(p)"

# ---------- Create app directory and non-root user ----------
WORKDIR /app

# ---------- Copy service code ----------
COPY shakemap_service /app/shakemap_service
COPY entrypoint.sh /app/entrypoint.sh
COPY scripts/verify-shakemap-image.sh /app/scripts/verify-shakemap-image.sh

RUN chmod +x /app/entrypoint.sh /app/scripts/verify-shakemap-image.sh

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
      --natural-earth-manifest /opt/shakemap-support/natural-earth-v5.1.2.json \
      --cartopy-data-dir /opt/shakemap-support/cartopy \
 && chmod -R a+rX /opt/shakemap-support \
 && chmod -R a-w /opt/shakemap-support \
 && chmod 0444 /opt/shakemap-build/identity.json /opt/shakemap-build/dependencies.txt

# ---------- Create non-root user and fix ownership ----------
RUN groupadd -g 1000 sysop && useradd -u 1000 -g 1000 -ms /bin/bash sysop \
 && mkdir -p "${RUNTIME_ROOT}" \
 && chown -R sysop:sysop "${RUNTIME_ROOT}" /app /opt/shakemap

# ---------- Image provenance labels ----------
LABEL org.opencontainers.image.created="${BUILD_TIMESTAMP_UTC}" \
      org.opencontainers.image.revision="${SERVICE_SOURCE_COMMIT}" \
      org.usgs.shakemap.source="${SHAKEMAP_SOURCE_URL}" \
      org.usgs.shakemap.release="${SHAKEMAP_RELEASE_TAG}" \
      org.usgs.shakemap.version="${SHAKEMAP_RELEASE_VERSION}" \
      org.usgs.shakemap.commit="${SHAKEMAP_SOURCE_COMMIT}" \
      org.usgs.shakemap.service.worktree-dirty="${SERVICE_WORKTREE_DIRTY}" \
      org.usgs.shakemap.identity-file="/opt/shakemap-build/identity.json" \
      org.usgs.shakemap.dependency-inventory="/opt/shakemap-build/dependencies.txt" \
      org.usgs.shakemap.natural-earth.tag="v5.1.2" \
      org.usgs.shakemap.natural-earth.commit="f1890d9f152c896d250a77557a5751a93d494776" \
      org.usgs.shakemap.natural-earth.manifest="/opt/shakemap-support/natural-earth-v5.1.2.json" \
      org.usgs.shakemap.cartopy-data="/opt/shakemap-support/cartopy"

USER sysop

# ---------- Networking ----------
EXPOSE 9010

ENTRYPOINT ["/app/entrypoint.sh"]
