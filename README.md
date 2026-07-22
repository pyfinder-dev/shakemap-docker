# ShakeMap Docker

This project packages an immutable official USGS ShakeMap release in a Docker
image and exposes the service through HTTP. Scientific datasets and regional
configuration remain outside the image in the mounted runtime directory.

Run the commands below from the `shakemap-docker` repository root.

## Prerequisites

- Docker with enough space for the ShakeMap image and dependencies.
- Git and network access to the official USGS GitLab service and Python package
  indexes during a build.
- The existing project environment in the parent directory. Activate it with:

  ```bash
  source ../.venv/bin/activate
  ```

Do not create another environment for this repository.

## Build

```bash
source ../.venv/bin/activate
./scripts/build-shakemap-docker.sh
```

The helper resolves the latest final stable release of the upstream USGS
ShakeMap software and builds `shakemap-docker:latest`. Most users should not
provide release options. The helper rejects moving branches and prereleases
and prints the ShakeMap release it selected during the build.

### Advanced: reproduce an exact ShakeMap build

The following optional values identify the upstream USGS ShakeMap source, not
this Docker project or a Docker image:

- `--release-tag` is a USGS ShakeMap Git release tag, such as `v4.4.9`.
- `--release-commit` is the full 40-character USGS ShakeMap Git commit to which
  that tag resolves.

Use the pair only when rebuilding a previously resolved ShakeMap release. You
can recover it from any of these sources:

- the original build output, shown as `Release tag` and `Source commit`;
- a running service's `/config` response, under
  `identity.immutable_image.upstream.release_tag` and
  `identity.immutable_image.upstream.source_commit`;
- an existing local image, using:

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

The helper checks that the recorded commit still matches the official USGS
tag before Docker builds the image. Supplying only one value, a shortened
commit, or a mismatched pair is rejected.

## Prepare the runtime and scientific data

```bash
mkdir -p ./runtime/shakemap/data
```

Place compatible ShakeMap datasets and configuration under
`./runtime/shakemap/data/`. The image does not contain the large scientific
datasets needed for a real calculation. See [Configuration](docs/configuration.md)
and [Runtime Layout](docs/runtime-layout.md) for the current paths.

## Start

```bash
./scripts/start-shakemap-docker.sh
```

This creates the stable `shakemap-docker` container from
`shakemap-docker:latest`, mounts `./runtime`, and publishes the API on port
9010. If a container with that name already exists, the helper preserves it
and tells you whether to start it or remove it explicitly.

## Check status

```bash
curl -fsS http://localhost:9010/config | python -m json.tool
curl -fsS http://localhost:9010/healthz | python -m json.tool
```

`/config` reports the active paths and immutable/deployment identity.
`/healthz` reports infrastructure and scientific readiness. A new container
may correctly report `not_ready` when compatible external data or configuration
is missing.

Image verification, imports, `/config`, and `/healthz` do not prove a real
ShakeMap calculation. The release-matched real-calculation verification
workflow is not yet complete. Uniform VS30 is only a development or emergency
override and cannot establish production or deployment readiness. A
`healthy_with_overrides` response must not be treated as production readiness.

## Stop and resume

Stop the container without deleting it:

```bash
docker stop shakemap-docker
```

Resume the same container and mounted runtime:

```bash
docker start shakemap-docker
```

## Rebuild and recreate the container

Rebuilding `shakemap-docker:latest` does not update an existing container.
Build the updated image, then explicitly replace only the container:

```bash
source ../.venv/bin/activate
./scripts/build-shakemap-docker.sh
docker stop shakemap-docker
docker rm shakemap-docker
./scripts/start-shakemap-docker.sh
```

Removing the container does not remove the bind-mounted `./runtime` directory.
Deleting the image is optional and is not part of normal build, start, or
status-check usage.

## Inspect failures

```bash
docker ps -a --filter name=shakemap-docker
docker logs shakemap-docker
curl -fsS http://localhost:9010/config | python -m json.tool
curl -fsS http://localhost:9010/healthz | python -m json.tool
```

For a stopped stable container, use `docker start shakemap-docker`. To recreate
it, first run `docker rm shakemap-docker`; the start helper never removes it
automatically.

## Runtime layout

The mounted service root is `./runtime/shakemap/` on the host:

```text
runtime/shakemap/
├── incoming/
├── products/
├── logs/
├── data/
└── .service/
    ├── events/
    ├── work/
    └── archive/
```

The top-level `incoming/`, `products/`, `logs/`, and `data/` directories are
user-facing. `.service/` contains internal state.

## Image and deployment identity

The read-only `/opt/shakemap-build/identity.json` manifest records the exact
upstream release, installed distributions, dependency inventory, service source
state, and build time. Docker image ID and repository digest are deployment
facts supplied by the start helper. `/config` and `/healthz` expose both kinds
of identity.

Do not pass these helper-managed variables through `--env`:

- `SHAKEMAP_IMAGE_ID`
- `SHAKEMAP_IMAGE_DIGEST`
- `SHAKEMAP_BUILD_IDENTITY_FILE`

The helper rejects them before starting Docker.

## More documentation

- [Quick Start](docs/quick-start.md) — complete operator lifecycle.
- [Scripts](scripts/README.md) — script purposes and options.
- [Developer Guide](docs/developer-guide.md) — host, container, and service QA.
- [REST API](docs/rest-api.md) — current HTTP surface.
- [Troubleshooting](docs/troubleshooting.md) — failure diagnosis.

The planned host command is `shake-docker`; it is distinct from the
`shakemap-docker` container. The public host CLI is not implemented yet, so use
the HTTP endpoints for current operator inspection.

## License

See [LICENSE](LICENSE).
