# Developer verification guide

Use the existing environment for every host Python command:

```bash
source ../.venv/bin/activate
```

## Host layer

```bash
python -m py_compile shakemap_service/*.py scripts/*.py tests/*.py
for file in scripts/*.sh entrypoint.sh; do bash -n "$file"; done
for file in tests/test_*.py; do python "$file"; done
git diff --check
```

Host tests must cover valid-data download avoidance, missing/corrupt/partial
data, manual placement, sibling-temporary and atomic placement, permissions,
manifest validation, known-package migration, documentation references, and
California package scope. Host success proves no container behavior.

## Image layer

Build a collision-resistant QA tag, leaving `shakemap-docker:latest` unchanged:

```bash
./scripts/build-shakemap-docker.sh --tag shakemap-docker:qa-<unique>
docker run --rm --network none \
  --entrypoint /app/scripts/verify-shakemap-image.sh \
  shakemap-docker:qa-<unique>
```

The verifier checks immutable ShakeMap identity, all Natural Earth checksums,
offline Cartopy resolution, installed STREC version/database digest and symlink,
imports, modules, user identity, and the supported script inventory.

## Preparation/native layer

Run `configure-shakemap.sh` against an isolated runtime and QA image. After
host downloads/imports, the preparation container has no network and mounts
scientific data read-only. Require both fixed default-plan executions to pass.
Retain the exact command, stdout/stderr, module order, configuration/data
inventories, and output inventory beneath the isolated runtime.

## Running-service layer

Start an isolated container, port, and prepared runtime, then run:

```bash
./scripts/verify-shakemap-deployment.sh \
  --url http://localhost:<qa-port> --expect ready
```

This checks public preparation reporting only. It intentionally does not test
or claim later queue, submission, CLI, concurrency, regional, archival, or
authoritative product-success capabilities.

Remove only exact temporary containers created for the check. Do not retag,
restart, remove, or recreate stable resources.
