FROM python:3.12-slim

# Release identity is resolved and validated by the host build helper.
ARG SHAKEMAP_SOURCE_URL
ARG SHAKEMAP_RELEASE_TAG
ARG SHAKEMAP_RELEASE_VERSION
ARG SHAKEMAP_SOURCE_COMMIT
ARG BUILD_TIMESTAMP_UTC

# Runtime environment
ENV PYTHONUNBUFFERED=1 \
    RUNTIME_ROOT=/home/sysop/runtime \
    SHAKEMAP_PORT=9010 \
    CARTOPY_DATA_DIR=/opt/shakemap-support/cartopy \
    SHAKEMAP_STREC_DB=/opt/shakemap-support/strec/moment_tensors.db

# System packages needed to fetch and install ShakeMap and its native dependencies.
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
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

# Install the release and its declared Python dependencies.
RUN pip install --no-cache-dir /opt/shakemap

# Copy the service identity implementation early so the resolved release's own
# lock can supply plotting compatibility. This is release-derived: future
# stable releases provide their own value or fail closed for explicit review.
WORKDIR /app
COPY shakemap_service /app/shakemap_service
COPY pyproject.toml README.md LICENSE /app/
COPY VERSIONS.env /opt/shakemap-build/VERSIONS.env
RUN python -m shakemap_service.build_identity apply-upstream-mapping-compatibility \
      --source /opt/shakemap \
      --output /opt/shakemap-build/mapping-compatibility.json

# Install the service last, then validate the complete installed environment.
RUN pip install --no-cache-dir /app \
 && python -m pip check

# Natural Earth is generic mapping support, not event/scenario data. The
# installer verifies every file against an immutable commit and SHA-256.
COPY image-support/natural-earth-v5.1.2.json /opt/shakemap-support/natural-earth-v5.1.2.json
COPY image-support/slab2.json /opt/shakemap-support/slab2.json
COPY scripts/install-image-support.py /tmp/install-image-support.py
RUN python /tmp/install-image-support.py natural-earth \
      --manifest /opt/shakemap-support/natural-earth-v5.1.2.json \
      --destination /opt/shakemap-support/cartopy \
 && python /tmp/install-image-support.py slab2 \
      --manifest /opt/shakemap-support/slab2.json \
      --destination /opt/shakemap-support/slab2 \
 && rm /tmp/install-image-support.py \
 && mkdir -p /opt/shakemap-support/strec \
 && python -c "import importlib.metadata,pathlib; d=importlib.metadata.distribution('usgs-strec'); p=next(d.locate_file(f) for f in d.files if str(f).endswith('strec/data/moment_tensors.db')); pathlib.Path('/opt/shakemap-support/strec/moment_tensors.db').symlink_to(p)"

# Repository configurations are immutable image seeds. Runtime materialization
# and native profile placement are deliberately handled by finalization.
COPY regional-configs /opt/shakemap-seeds/regional
COPY verification/scenarios/v4.4.9/south-napa-global/event.xml \
     verification/scenarios/v4.4.9/south-napa-global/event_dat.xml \
     verification/scenarios/v4.4.9/south-napa-global/scenario-manifest.json \
     /opt/shakemap-verification/
COPY verification/packages/v4.4.9/source-manifest.json \
     /opt/shakemap-verification/source-manifest.json

COPY entrypoint.sh /app/entrypoint.sh
COPY scripts/verify-shakemap-image.sh /app/scripts/verify-shakemap-image.sh

RUN chmod +x /app/entrypoint.sh /app/scripts/verify-shakemap-image.sh

# Capture a complete freeze before sorting so a failed inventory command cannot
# leave a partial dependency record that appears successful.
RUN mkdir -p /opt/shakemap-build \
 && dependencies_tmp="$(mktemp /opt/shakemap-build/dependencies.XXXXXX)" \
 && python -m pip freeze --all > "${dependencies_tmp}" \
 && LC_ALL=C sort "${dependencies_tmp}" > /opt/shakemap-build/dependencies.txt \
 && rm "${dependencies_tmp}" \
 && python -m shakemap_service.build_identity write \
      --output /opt/shakemap-build/identity.json \
      --dependencies /opt/shakemap-build/dependencies.txt \
      --source-url "${SHAKEMAP_SOURCE_URL}" \
      --release-tag "${SHAKEMAP_RELEASE_TAG}" \
      --release-version "${SHAKEMAP_RELEASE_VERSION}" \
      --source-commit "${SHAKEMAP_SOURCE_COMMIT}" \
      --build-timestamp-utc "${BUILD_TIMESTAMP_UTC}" \
      --natural-earth-manifest /opt/shakemap-support/natural-earth-v5.1.2.json \
      --cartopy-data-dir /opt/shakemap-support/cartopy \
      --slab2-manifest /opt/shakemap-support/slab2.json \
      --slab2-support-dir /opt/shakemap-support/slab2 \
      --mapping-compatibility-record /opt/shakemap-build/mapping-compatibility.json \
 && chmod -R a+rX /opt/shakemap-support \
 && chmod -R a-w /opt/shakemap-support \
 && chmod -R a+rX /opt/shakemap-seeds \
 && chmod -R a-w /opt/shakemap-seeds \
 && chmod -R a+rX /opt/shakemap-verification \
 && chmod -R a-w /opt/shakemap-verification \
 && chmod 0444 /opt/shakemap-build/identity.json /opt/shakemap-build/dependencies.txt \
      /opt/shakemap-build/mapping-compatibility.json

# Create the fixed non-root runtime identity.
RUN groupadd -g 1000 sysop && useradd -u 1000 -g 1000 -ms /bin/bash sysop \
 && mkdir -p "${RUNTIME_ROOT}" \
 && chown -R sysop:sysop "${RUNTIME_ROOT}" /app /opt/shakemap

# Compact labels expose only the identity needed before reading the full manifest.
LABEL org.opencontainers.image.created="${BUILD_TIMESTAMP_UTC}" \
      org.usgs.shakemap.release="${SHAKEMAP_RELEASE_TAG}" \
      org.usgs.shakemap.version="${SHAKEMAP_RELEASE_VERSION}" \
      org.usgs.shakemap.commit="${SHAKEMAP_SOURCE_COMMIT}"

USER sysop

EXPOSE 9010

ENTRYPOINT ["/app/entrypoint.sh"]
