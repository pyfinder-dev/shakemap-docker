# Scripts

| Script | Responsibility |
|---|---|
| `build-shakemap-docker.sh` | Build the declared release as an untagged candidate, verify it by identity, and then promote the canonical image tag. |
| `manage-shakemap-data.sh` | Inspect, validate, provision missing global VS30/topography assets, or stage validated replacements. |
| `prepare-shakemap-verification-data.py` | Prepare or validate a release-matched fixed verification package. |
| `fix-shakemap-permissions.sh` | Add read/traversal access to selected operator-owned scientific data, preserving ownership and write bits. |
| `repair-shakemap-writable-paths.sh` | Repair ownership and owner access only within the four service-writable trees; reused by finalization and manual privileged recovery. |
| `finalize-shakemap.sh` | Prepare the runtime, verify effective container access, run the fixed global calculation, and publish readiness after successful checks. |
| `start-shakemap-docker.sh` | Start only an already-finalized deployment matching the canonical image, mounts, and settings. |
| `stop-shakemap-docker.sh` | Stop the canonical service gracefully while retaining its container and runtime. |
| `verify-shakemap-image.sh` | Verify installed image identity, imports, and immutable support data. |
| `verify-shakemap-deployment.sh` | Run host tests, container checks, and running-service REST/CLI checks, including the fixed global calculation. |
| `container-configuration.sh` | Shared canonical identity, mount construction, and configuration checks used by deployment helpers. |
| `install-image-support.py` | Install pinned immutable support assets during the image build. |

`manage-shakemap-data.sh inspect` is cheap and read-only.
`manage-shakemap-data.sh validate` performs full pinned checksum validation and
may take time. `manage-shakemap-data.sh provision` reuses a valid existing asset
and installs only a missing one. It never replaces an invalid or unexpected
existing asset.

Install the standard project package in the project environment before running
the build helper. The helper checks this prerequisite and never installs it:

```bash
source /path/to/project/.venv/bin/activate
python -m pip install -e .
./scripts/build-shakemap-docker.sh
```

Keep that environment active before running the data helper:

```bash
source /path/to/project/.venv/bin/activate
./scripts/manage-shakemap-data.sh inspect
```

Deployment helpers accept `--runtime-root DIR`, `--port PORT`, and
`--max-concurrent COUNT`. There is one canonical image and container, both
named `shakemap-docker` (image tag `latest`). The runtime mounts at
`/home/sysop/runtime`; the `shakemap/data/global`, `regional`, and `test`
subtrees have read-only overlays. The start helper has no separate `--data`,
`--runtime`, or container-name override.

`make build`, `data`, `fix-permissions`, `finalize`, `start`, `stop`, and
`verify` are thin aliases to the corresponding helpers. The writable repair
script is a recovery command, not an additional installation stage. It uses
host shell utilities and needs neither Docker nor a Python environment.
See [permissions](../docs/permissions.md) for its preconditions and manual
`sudo` usage. Never elevate the entire finalization helper.
