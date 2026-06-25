# Permissions Guide

This guide covers file ownership, the container user, and platform-specific permission behavior.

For the quick fix, see the [Volume Mounts and Permissions](../README.md#volume-mounts-and-permissions) section in the README.

---

## Container User

The ShakeMap Docker container runs as:

| Property | Value |
|----------|-------|
| Username | `sysop` |
| UID | 1000 |
| GID | 1000 |
| Home | `/home/sysop` |

This user is created during the Docker image build. The container does **not** run as root.

---

## Volume Mount Setup

The recommended volume mount maps the host runtime directory to `RUNTIME_ROOT` inside the container:

```bash
docker run -v ./runtime:/home/sysop/runtime ...
```

Or via the start script:

```bash
./scripts/start-shakemap-docker.sh --runtime ./runtime
```

The `sysop` user (UID 1000) must be able to write to this directory.

---

## Platform-Specific Behavior

### Linux

On Linux, Docker bind mounts preserve the host filesystem's ownership and permissions. The container process sees the exact UID/GID of the host files.

**Requirement:** The host directory must be owned by UID 1000, GID 1000 (matching the `sysop` user inside the container).

**Fix:**

```bash
chown -R 1000:1000 ./runtime
```

If the host directory is owned by a different user, the `sysop` user cannot write to it, and the container will fail at startup with an actionable error message.

### macOS (Docker Desktop)

Docker Desktop on macOS uses a Linux VM with a filesystem bridge (gRPC-FUSE or VirtioFS). Files in mounted directories typically appear owned by the container user regardless of host ownership.

**In practice:** Permissions usually work without `chown`. Docker Desktop handles the UID mapping transparently.

**Edge cases:** With some VirtioFS configurations, read-only host directories may still appear writable to the container. The entrypoint's writability test (`touch` a file, then remove it) catches most issues, but false positives are possible in rare configurations.

### Windows (Docker Desktop)

Docker Desktop on Windows uses WSL2 or Hyper-V. Mounted files typically appear with synthetic permissions (`0777`) and ownership (`root:root` or the container user).

**In practice:** Permissions usually work without `chown`. The synthetic permissions are permissive.

**Known limitation:** The `fcntl.flock` file locking used by the queue system may not work correctly on CIFS/SMB-mounted paths on Windows bind mounts. This affects concurrent claim safety but not single-worker operation. For production deployments, Linux is recommended.

---

## Entrypoint Permission Detection

The entrypoint (`entrypoint.sh`) performs active writability testing at startup:

1. For each of the six service directories (`events/`, `incoming/`, `work/`, `products/`, `archive/`, `logs/`):
   - Attempts to create a test file (`touch .writetest_$$`)
   - Removes the test file on success
   - **Fails with an actionable error** on failure

2. If any directory is not writable, the error message includes:
   - The directory path
   - The current owner (UID:GID)
   - The required owner (1000:1000)
   - A suggested `chown` command

The entrypoint also attempts `chmod 0755` on each directory as a best-effort step, but this is a **no-op on bind mounts** on all platforms (the container user cannot change permissions on host-owned directories). The actual writability is determined by the `touch` test.

**Example error output:**

```
[entrypoint] ERROR: /home/sysop/runtime/shakemap/events is not writable.

  Directory:      /home/sysop/runtime/shakemap/events
  Current owner:  0:0
  Required owner: 1000:1000

  Suggested fix:
    chown -R 1000:1000 <host-runtime-dir>
```

---

## Troubleshooting Permissions

| Symptom | Cause | Fix |
|---------|-------|-----|
| Entrypoint exits with "not writable" | Host directory owned by wrong UID | `chown -R 1000:1000 ./runtime` |
| `configure-shakemap.sh` fails to write sentinel | `~/.shakemap/` not writable | This directory is inside the container, not a bind mount. Should not fail unless the image is corrupted. |
| Products not written after ShakeMap success | `products/` directory permissions | Check that `products/` under the mount is writable. `chown -R 1000:1000 ./runtime` |
| Queue locking errors on Windows | `fcntl.flock` not supported on CIFS mounts | Use Linux for production, or run with a single worker |

---

## Related Documentation

- [Runtime Layout](runtime-layout.md) — directory structure and volume mounts
- [Troubleshooting](troubleshooting.md) — common problems and fixes
- [Configuration Guide](configuration.md) — `SHAKEMAP_REQUIRE_MOUNT` option
