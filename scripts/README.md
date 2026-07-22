# Script Reference

Run host scripts from the `shakemap-docker` repository root after activating
the existing project environment with `source ../.venv/bin/activate`.

## Operator-facing scripts

### `build-shakemap-docker.sh`

Builds `shakemap-docker:latest` from the latest final stable release of the
upstream USGS ShakeMap software. Most users should omit the release override
options and let the helper select and print the verified release automatically.

| Option | Meaning | Default |
|---|---|---|
| `--tag IMAGE` | Image name and tag | `shakemap-docker:latest` |
| `--platform PLATFORM` | Docker target platform | Docker default |
| `--no-cache` | Disable Docker build cache | cache enabled |
| `--release-tag TAG` | Upstream USGS ShakeMap Git release tag; requires `--release-commit` | selected automatically |
| `--release-commit COMMIT` | Full 40-character USGS ShakeMap Git commit referenced by the release tag | selected automatically |
| `--help` | Show usage | — |

These options identify the upstream ShakeMap source, not this Docker project's
Git revision or the Docker image ID. They are intended only to reproduce an
exact release that was resolved previously. Recover the pair from the original
build output, `identity.immutable_image.upstream` in `/config`, or the image
labels `org.usgs.shakemap.release` and `org.usgs.shakemap.commit`. The override
pair is revalidated against the official tag. Moving branches, prereleases,
short commits, and mismatched pairs are rejected.

### `start-shakemap-docker.sh`

Creates the stable service container. It never removes an existing container.

| Option | Meaning | Default |
|---|---|---|
| `--name NAME` | Container name | `shakemap-docker` |
| `--runtime DIR` | Host runtime directory | `./runtime` |
| `--port PORT` | Host API port | `9010` |
| `--image IMAGE` | Image name and tag | `shakemap-docker:latest` |
| `--env KEY=VALUE` | Supported extra environment variable; repeatable | — |
| `--foreground` | Run attached | detached |
| `--detach` | Run detached | detached |
| `--help` | Show usage | — |

If the requested container is running, the script reports it and exits. If it
is stopped, the script recommends `docker start <name>` or an explicit
`docker rm <name>` before recreation.

`SHAKEMAP_IMAGE_ID`, `SHAKEMAP_IMAGE_DIGEST`, and
`SHAKEMAP_BUILD_IDENTITY_FILE` are helper-managed and rejected through
`--env`. Other valid `KEY=VALUE` settings remain supported.

## Developer and internal scripts

| Script | Where it runs | Purpose |
|---|---|---|
| `verify-shakemap-image.sh` | container | Check image identity, installed modules, paths, and permissions |
| `verify-shakemap-deployment.sh` | host | Check an explicitly named running container and expected readiness state |
| `verify-shakemap-build.sh` | container | Legacy infrastructure checks |
| `verify-shakemap-config.sh` | container | Legacy configuration checks |
| `configure-shakemap.sh` | container | Legacy profile/data configuration helper; does not guarantee readiness |
| `inspect-shakemap-config.sh` | container | Inspect current in-container configuration |
| `inspect-shakemap-events.sh` | container | Inspect current event state |
| `run-shakemap-ci-tests.sh` | host | Broader legacy integration workflow using an isolated test image |

These internal scripts are not a substitute for release-matched real
calculation verification. See the [Developer Guide](../docs/developer-guide.md)
for isolated QA commands and the three verification layers.
