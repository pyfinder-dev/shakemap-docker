# Developer Guide

Run commands from the `shakemap-docker` repository root. Activate the existing
project environment first:

```bash
source ../.venv/bin/activate
```

Do not create another virtual environment and do not use system Python for
project tests.

## Verification layers

Keep host tests, container-internal checks, and running-service checks separate.
Passing one layer does not imply that the next layer passes, and none of these
checks alone proves a real ShakeMap calculation.

### 1. Host-side tests

```bash
python -m py_compile \
  shakemap_service/release.py \
  shakemap_service/build_identity.py \
  tests/test_release_resolution.py \
  tests/test_build_identity.py \
  tests/test_container_startup.py
bash -n scripts/build-shakemap-docker.sh
bash -n scripts/start-shakemap-docker.sh
bash -n scripts/verify-shakemap-image.sh
python tests/test_release_resolution.py
python tests/test_build_identity.py
python tests/test_container_startup.py
git diff --check
```

These tests cover stable release resolution, immutable/deployment identity,
stable operator defaults, reserved variables, normal environment propagation,
and preservation of existing running or stopped containers.

### 2. Container-internal checks

Use isolated QA names so a developer check cannot modify the stable operator
container or runtime:

```bash
export QA_IMAGE="shakemap-docker:integration-test"
export QA_CONTAINER="shakemap-docker-qa"
export QA_RUNTIME="/private/tmp/shakemap-docker-qa-runtime"
export QA_PORT="19010"

./scripts/build-shakemap-docker.sh --tag "$QA_IMAGE"
./scripts/start-shakemap-docker.sh \
  --name "$QA_CONTAINER" \
  --runtime "$QA_RUNTIME" \
  --port "$QA_PORT" \
  --image "$QA_IMAGE"
docker exec "$QA_CONTAINER" /app/scripts/verify-shakemap-image.sh
```

The internal verifier checks the fixed manifest, checkout, installed versions,
native ShakeMap version, dependency inventory, service imports, routes, runtime
layout, and permissions. It does not execute the release-matched real
calculation workflow.

Using `docker exec` is acceptable for developer verification. Routine users
should not need it after the planned `shake-docker` host CLI is implemented.
That public CLI does not exist yet; current operator inspection uses REST.

### 3. Running-service checks

```bash
curl -fsS "http://localhost:${QA_PORT}/config" | python -m json.tool
curl -fsS "http://localhost:${QA_PORT}/healthz" | python -m json.tool
python -c 'import json, os, urllib.request as u; p=os.environ["QA_PORT"]; c=json.load(u.urlopen(f"http://localhost:{p}/config")); h=json.load(u.urlopen(f"http://localhost:{p}/healthz")); assert c["identity"] == h["identity"]'
```

Check that immutable identity matches the manifest/labels, deployment identity
matches Docker inspection, and scientific readiness is reported separately.
`not_ready` is a valid result when external data or configuration is absent.

The current project has not completed release-matched real-calculation
verification. Do not use uniform VS30, the minimal synthetic fixture, imports,
or existing product files as proof of deployment readiness. Successful work
requires validated core products, provenance, a product manifest, and logs.

### Clean up isolated QA resources

Confirm the variables still name only the QA resources before cleanup:

```bash
test "$QA_CONTAINER" = "shakemap-docker-qa"
test "$QA_RUNTIME" = "/private/tmp/shakemap-docker-qa-runtime"
docker inspect "$QA_CONTAINER" --format '{{.Name}} {{.Config.Image}} {{range .Mounts}}{{.Source}}{{end}}'
docker rm -f "$QA_CONTAINER"
rm -rf -- "$QA_RUNTIME"
```

Removing the QA image is optional. Never delete or replace
`shakemap-docker:latest`, `shakemap-docker`, or `./runtime` as part of QA.

## Resource naming

- Stable operator image: `shakemap-docker:latest`
- Stable operator container: `shakemap-docker`
- Future host CLI: `shake-docker`
- Isolated QA names: explicit `*-qa` or `*:integration-test` resources

QA names prevent collisions; they are not user-facing defaults.

## Code map

| Path | Responsibility |
|---|---|
| `Dockerfile` | Immutable image construction and build manifest |
| `entrypoint.sh` | Runtime layout checks and service startup |
| `shakemap_service/release.py` | Stable official release resolution |
| `shakemap_service/build_identity.py` | Build manifest writing/loading and deployment identity |
| `shakemap_service/main.py` | FastAPI application and inspection endpoints |
| `shakemap_service/paths.py` | Runtime path definitions |
| `shakemap_service/status.py` | Current durable status model |
| `shakemap_service/submission.py` | Current submission/staging behavior |
| `shakemap_service/queue.py` | Current filesystem queue behavior |
| `shakemap_service/worker.py` | Current worker behavior |
| `shakemap_service/runner.py` | Current native ShakeMap bridge and publication checks |

Some queue, retry, region, fixture, calculation, concurrency, recalculation,
and CLI behavior remains scheduled for later work. Do not infer completion from
the presence of current modules or legacy tests.

## Editing rules

- Keep runtime path construction in `shakemap_service/paths.py`.
- Preserve the user-facing `incoming/`, `products/`, `logs/`, and `data/`
  top-level layout and internal `.service/events/`, `.service/work/`, and
  `.service/archive/` state.
- Use atomic writes for durable status and metadata.
- Keep REST and the future host CLI on the same validated capability model.
- Require validated core products, provenance, product manifest, and logs
  before reporting calculation success.

## Related documentation

- [Operator Quick Start](quick-start.md)
- [Script Reference](../scripts/README.md)
- [Runtime Layout](runtime-layout.md)
- [REST API](rest-api.md)
- [Troubleshooting](troubleshooting.md)
