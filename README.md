# ShakeMap Docker service

This repository builds an immutable ShakeMap image and exposes read-only
identity, configuration discovery, external-data inspection, and health APIs.
Managed calculation execution is intentionally disabled: effective ShakeMap
configuration resolution and authoritative success semantics are not yet
implemented.

## Supported workflow

```bash
./scripts/build-shakemap-docker.sh
./scripts/manage-shakemap-data.sh inspect
./scripts/manage-shakemap-data.sh validate
./scripts/start-shakemap-docker.sh
curl -fsS http://localhost:9010/config
curl -fsS http://localhost:9010/healthz
```

`inspect` is cheap and read-only. It checks presence/readability and parses only
small directory entries. `validate` is also read-only, but hashes the two large
pinned global assets. `provision` reuses valid assets and may download or import
only a missing asset. An invalid, incomplete, or unexpected existing asset is
left unchanged and reported with corrective action.

Activate the project Python environment before running the data helper.

## Runtime data contract

External data lives below `runtime/shakemap/data/`:

```text
global/
  vs30/global_vs30.grd
  topo/topo_30sec.grd
regional/
test/<resolved-version>/
```

Stage 2 manages only the displayed global VS30 and topography files. Other
small support data belongs to image build or finalization, not this helper.
Manual placement remains supported. For a separate state directory using an
existing data tree:

```bash
./scripts/start-shakemap-docker.sh \
  --runtime /path/to/disposable-runtime \
  --data /path/to/existing/shakemap/data
```

The data mount is read-only. The service does not create or require
`.service/preparation`, `incoming`, `.service/work`, or a top-level event/work
tree.

## What API evidence means

`/config` and `/healthz` use response schema `"1.0"`. They distinguish:

- filesystem presence and readability;
- structural and checksum validation;
- release compatibility and coverage;
- whether managed execution used an asset;
- managed-execution and overall readiness.

Presence is not validation, manifest-declared compatibility is not checksum
validation, and neither implies scientific coverage or calculation readiness.
Overall readiness remains false while managed execution is disabled.

See [docs/quick-start.md](docs/quick-start.md),
[docs/runtime-layout.md](docs/runtime-layout.md), and
[docs/health-and-readiness.md](docs/health-and-readiness.md).
