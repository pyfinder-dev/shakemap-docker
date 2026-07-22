# Operator Quick Start

Run every command from the `shakemap-docker` repository root. This guide keeps
the stable image, container, runtime, and port throughout normal operation:

- image: `shakemap-docker:latest`
- container: `shakemap-docker`
- runtime: `./runtime`
- API: `http://localhost:9010`

## 1. Check prerequisites

```bash
source ../.venv/bin/activate
docker info
```

The build needs network access to the official USGS GitLab service and Python
package indexes. Use the existing parent project environment; do not create a
new one.

## 2. Build and retain the image

```bash
./scripts/build-shakemap-docker.sh
```

The result is `shakemap-docker:latest`, built from the latest final stable
release of the upstream USGS ShakeMap software and its verified full Git
commit. Most users should not provide release options; the build helper
selects and prints both values automatically.

For a reproducible rebuild of an already resolved ShakeMap release,
`--release-tag` is the upstream USGS ShakeMap Git tag and
`--release-commit` is the full 40-character USGS ShakeMap commit referenced by
that tag. They do not identify this Docker project or a Docker image.

Recover the pair from the original build output, from
`identity.immutable_image.upstream` in a running service's `/config` response,
or from an existing local image:

```bash
docker image inspect shakemap-docker:latest \
  --format '{{ index .Config.Labels "org.usgs.shakemap.release" }} {{ index .Config.Labels "org.usgs.shakemap.commit" }}'
```

Then pass both recorded values:

```bash
./scripts/build-shakemap-docker.sh \
  --release-tag vX.Y.Z \
  --release-commit 0123456789abcdef0123456789abcdef01234567
```

Both values are required together and are checked against the official USGS
tag. Branches, prereleases, short commits, and mismatched pairs are rejected.

## 3. Prepare persistent runtime data

```bash
mkdir -p ./runtime/shakemap/data
```

Provide release-compatible scientific datasets and configuration beneath
`./runtime/shakemap/data/`. These large files are intentionally outside the
image. The current data locations are described in
[Configuration](configuration.md) and [Runtime Layout](runtime-layout.md).

Do not use uniform VS30 as a normal setup path. It is only a development or
emergency override and cannot demonstrate production or deployment readiness.

## 4. Start the stable container

```bash
./scripts/start-shakemap-docker.sh
```

The helper starts `shakemap-docker` in the background, mounts `./runtime` at
`/home/sysop/runtime`, and publishes port 9010.

If `shakemap-docker` already exists, the helper exits without modifying it:

- running container: leave it running or stop it explicitly;
- stopped container: resume it with `docker start shakemap-docker`;
- replacement: remove it explicitly with `docker rm shakemap-docker`, then run
  the start helper again.

## 5. Check configuration and readiness

```bash
curl -fsS http://localhost:9010/config | python -m json.tool
curl -fsS http://localhost:9010/healthz | python -m json.tool
```

These checks are non-destructive. `/config` shows paths, configuration, and
image/deployment identity. `/healthz` may correctly report `not_ready` while
scientific data or configuration is absent or incompatible.

Do not infer calculation readiness from a successful image build, imports,
image verification, `/config`, or `/healthz`. The project has not yet completed
its release-matched real-calculation verification workflow. The repository's
minimal synthetic fixture does not prove real ShakeMap execution.

## 6. Stop and resume

Stop without deleting the container or runtime:

```bash
docker stop shakemap-docker
```

Resume the same container:

```bash
docker start shakemap-docker
```

## 7. Rebuild or update

An existing container continues to use the image from which it was created.
Rebuilding the tag alone does not update that container.

```bash
source ../.venv/bin/activate
./scripts/build-shakemap-docker.sh
docker stop shakemap-docker
docker rm shakemap-docker
./scripts/start-shakemap-docker.sh
```

This removes only the old container. The bind-mounted `./runtime` directory
and its data remain. Removing `shakemap-docker:latest` is optional and is not a
normal update step.

## 8. Inspect a failure

```bash
docker ps -a --filter name=shakemap-docker
docker logs shakemap-docker
curl -fsS http://localhost:9010/config | python -m json.tool
curl -fsS http://localhost:9010/healthz | python -m json.tool
```

Use the reported blocking reasons and active paths to correct data,
configuration, permissions, or port conflicts. See
[Troubleshooting](troubleshooting.md) for more diagnostics.

## Makefile equivalents

```bash
make build
make start
make verify
```

The defaults are the same stable resources. `make verify` expects the honest
fresh-container state `not-ready`; override `EXPECT` only when the deployment
has compatible scientific data and configuration.
