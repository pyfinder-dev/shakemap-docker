# Script inventory

## Supported operator-facing

| Script | Purpose |
|---|---|
| `build-shakemap-docker.sh` | Resolve one official stable release and build the image. |
| `configure-shakemap.sh` | Prepare and validate the mounted runtime before startup. |
| `start-shakemap-docker.sh` | Start the service while preserving an existing container. |
| `verify-shakemap-deployment.sh` | Verify `/config` and `/healthz` through public HTTP. |

## Supported internal verification

| Script | Purpose |
|---|---|
| `verify-shakemap-image.sh` | Verify image identity, generic support, imports, and modules. |
| `prepare-shakemap-verification-data.py` | Prepare, migrate, validate, or run the California package. |
| `install-image-support.py` | Build-time installer for pinned Natural Earth files. |

The removed stage verifiers, container inspectors, and broad CI script encoded
the obsolete start-then-configure lifecycle and development override behavior.
The preparation report, public endpoints, image verifier, and capability tests
replace them.

Operator scripts require Python 3.10 or newer and only its standard library.
They do not require a repository-adjacent virtual environment. Override the
interpreter name when necessary:

```bash
SHAKEMAP_HOST_PYTHON=/path/to/python3 ./scripts/configure-shakemap.sh
```

Repository development and test commands follow the separate project
environment policy in `AGENTS.md`.
