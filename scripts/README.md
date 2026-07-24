# Scripts

| Script | Responsibility |
|---|---|
| `build-shakemap-docker.sh` | Resolve and build one immutable upstream ShakeMap release. |
| `manage-shakemap-data.sh` | Inspect, fully validate, or explicitly provision external datasets. |
| `prepare-shakemap-verification-data.py` | Prepare or validate a release-matched fixed verification package. |
| `start-shakemap-docker.sh` | Start the stable service with isolated state and a read-only data mount. |
| `verify-shakemap-image.sh` | Verify installed image identity, imports, and immutable support data. |
| `verify-shakemap-deployment.sh` | Verify public schema, liveness, data evidence, and disabled readiness. |

`manage-shakemap-data.sh inspect` is cheap and read-only.
`manage-shakemap-data.sh validate` performs full pinned checksum validation and
may take time. `manage-shakemap-data.sh provision` is the only action here that
may alter scientific data.

Host-side Python helpers require Python 3.10 or newer:

```bash
SHAKEMAP_HOST_PYTHON=/path/to/python3 ./scripts/manage-shakemap-data.sh inspect
```

The start helper accepts `--data DIR` for an existing exact data tree. It mounts
that directory at `/home/sysop/runtime/shakemap/data:ro`; service-owned state
continues to use the separately selected `--runtime` directory.
