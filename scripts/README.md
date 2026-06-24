# ShakeMap Docker -- Scripts

## User Workflow

The intended deployment workflow is four commands:

```bash
./scripts/build-shakemap-docker.sh                          # 1. Build image
./scripts/start-shakemap-docker.sh                          # 2. Start container
docker exec shakemap /app/scripts/configure-shakemap.sh     # 3. Configure ShakeMap
./scripts/verify-shakemap-deployment.sh shakemap --expect ready  # 4. Verify
```

Or via Makefile:

```bash
make build
make start
make configure
make verify
```

### build-shakemap-docker.sh

Build the Docker image.

| Flag | Description | Default |
|------|-------------|---------|
| `--tag TAG` | Image name and tag | `shakemap-service:latest` |
| `--platform PLAT` | Target platform (e.g. `linux/amd64`) | current host platform |
| `--no-cache` | Build without layer cache | caching enabled |
| `--help` | Print usage and exit | -- |

### start-shakemap-docker.sh

Start the service container with sensible defaults.

| Flag | Description | Default |
|------|-------------|---------|
| `--name NAME` | Container name | `shakemap` |
| `--runtime DIR` | Host runtime directory | `./runtime` |
| `--port PORT` | Host port | `9010` |
| `--image IMAGE` | Image name:tag | `shakemap-service:latest` |
| `--env KEY=VALUE` | Extra env var (repeatable) | -- |
| `--foreground` | Run in foreground | detached |
| `--help` | Print usage and exit | -- |

### configure-shakemap.sh

Run inside the container after start. Configures the ShakeMap profile,
sets up data, and writes the readiness sentinel.

```bash
docker exec <container> /app/scripts/configure-shakemap.sh
```

Idempotent and safe to run multiple times.

#### Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `SHAKEMAP_PROFILE` | `default` | Profile name |
| `SHAKEMAP_SKIP_DATA_DOWNLOAD` | `0` | Set to `1` to skip USGS downloads |
| `SHAKEMAP_ALLOW_UNIFORM_VS30` | `0` | Set to `1` to allow empty vs30file |
| `SHAKEMAP_VS30_FILE` | -- | Custom path to VS30 grid |
| `SHAKEMAP_TOPO_FILE` | -- | Custom path to topography grid |

### verify-shakemap-deployment.sh

Verify a running container. Does NOT rebuild or create containers.

```bash
./scripts/verify-shakemap-deployment.sh <container> --expect not-ready   # after start
./scripts/verify-shakemap-deployment.sh <container> --expect ready       # after configure
```

### Health check

```
GET http://localhost:9010/healthz
```

Returns `status`, `blocking_reasons`, and `next_action`.

---

## Developer / Internal Tooling

### verify-shakemap-build.sh

Validates build/infrastructure guarantees (user identity, directories, CLI, health endpoint). Run inside the container.

```bash
docker exec <container> /app/scripts/verify-shakemap-build.sh
```

### verify-shakemap-config.sh

Validates configuration/readiness guarantees (profile, symlink, data, sentinel, idempotency). Run inside the container after configure-shakemap.sh.

```bash
docker exec <container> /app/scripts/verify-shakemap-config.sh
```

### run-shakemap-ci-tests.sh

Full automated CI test: builds the image, starts a container, runs build and config verification, checks submit gate behavior, and verifies idempotency. Run from the host.

```bash
./scripts/run-shakemap-ci-tests.sh
```

Exit code is non-zero if any check fails.
