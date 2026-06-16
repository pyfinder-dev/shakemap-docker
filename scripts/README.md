# ShakeMap Docker — Scripts

Helper scripts for building and managing the ShakeMap Docker image.

## build-docker.sh

Build the ShakeMap Docker image locally using `docker buildx build --load`.

### Usage

```bash
./scripts/build-docker.sh [OPTIONS]
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--tag TAG` | Image name and tag to assign to the built image. Must follow Docker naming rules (`name:tag`). | `shakemap-service:latest` |
| `--platform PLAT` | Target platform for the build (e.g. `linux/amd64`, `linux/arm64`). When omitted, Docker uses the current host platform. | *(current docker default)* |
| `--no-cache` | Build from scratch without using any cached layers. Useful after Dockerfile or dependency changes that the cache may not detect. | *(caching enabled)* |
| `--help`, `-h` | Print the embedded usage summary and exit. | — |

### Examples

**Default build** — uses `shakemap-service:latest` on the current platform:

```bash
./scripts/build-docker.sh
```

**Custom tag** — tag the image for a specific phase or test:

```bash
./scripts/build-docker.sh --tag shakemap-service:phase01
```

**Cross-platform build** — build for `linux/amd64` on an Apple Silicon Mac:

```bash
./scripts/build-docker.sh --platform linux/amd64
```

**Combined** — custom tag and platform:

```bash
./scripts/build-docker.sh --tag shakemap-service:test --platform linux/amd64
```

**Clean build** — discard all cached layers:

```bash
./scripts/build-docker.sh --no-cache
```

### Behaviour

- Uses `docker buildx build` with `--load` to make the image available locally.
- Prints the exact `docker buildx build` command before executing it.
- Fails fast with a clear error if:
  - A required argument value is missing.
  - An unknown flag is passed.
  - The `Dockerfile` cannot be found relative to the script.
  - `docker` is not installed or not in `PATH`.
- Never pushes images to a registry.
- Does not require Docker Compose.
- The build context is always the repository root (`shakemap-docker/`), resolved relative to the script location — so the script works from any working directory.
