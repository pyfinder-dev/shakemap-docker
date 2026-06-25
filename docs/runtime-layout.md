# Runtime Layout Guide

This guide documents the complete directory structure inside the ShakeMap Docker container, what each directory contains, and how to set up volume mounts for data persistence.

For the top-level overview, see the [Runtime Directory Layout](../README.md#runtime-directory-layout) section in the README.

---

## Service Directory Tree

The service root at `SERVICE_ROOT` (default: `/home/sysop/runtime/shakemap/`) contains six core directories plus a shared data directory:

```
/home/sysop/runtime/shakemap/         (SERVICE_ROOT)
├── events/                           Event tracking
│   └── <event_id>/
│       └── .shakemap-service/
│           └── requeststatus.json    Authoritative event status
├── incoming/                         Staged input files
│   └── <event_id>/
│       ├── event.xml
│       ├── event_dat.xml
│       └── rupture.json
├── work/                             ShakeMap processing (private)
│   └── <event_id>/
│       └── current/
│           ├── event.xml             Copied from incoming/
│           ├── event_dat.xml
│           └── products/             ShakeMap output (before publication)
├── products/                         Published outputs
│   └── <event_id>/
│       └── ...                       Atomically published ShakeMap products
├── archive/                          Completed-run archive
├── logs/                             Service logs
└── data/                             Shared data files
    ├── vs30/
    │   └── global_vs30.grd           VS30 grid (downloaded or mounted)
    └── topo/
        └── topo_30sec.grd            Topography grid
```

### Directory Purposes

| Directory | Contents | Created By |
|-----------|----------|------------|
| `events/` | Per-event tracking directories. Each event has a hidden `.shakemap-service/` subdirectory containing `requeststatus.json`, which is the authoritative record of the event's lifecycle. | Submission endpoint |
| `incoming/` | Staged input files for each submitted event. Files are written atomically (temp dir → rename) so consumers never see partial data. | Submission endpoint |
| `work/` | ShakeMap's private processing directory. Input files are copied here from `incoming/` before execution. This directory is also the target of the ShakeMap profile's `data` symlink. | Execution bridge |
| `products/` | Published ShakeMap outputs. After successful processing, products are atomically copied here from `work/<event_id>/current/products/`. | Execution bridge |
| `archive/` | Archive storage for completed runs. Reserved for future use. | Not yet implemented |
| `logs/` | Service log files. | Entrypoint |
| `data/` | Shared data files (VS30 grids, topography grids) downloaded or mounted during Stage 2 configuration. | Configure script |

---

## Per-Event File Lifecycle

When an event is submitted and processed, files move through the directory structure:

```
1. Submit    →  incoming/<event_id>/event.xml, event_dat.xml, ...
                events/<event_id>/.shakemap-service/requeststatus.json

2. Execute   →  work/<event_id>/current/event.xml, event_dat.xml, ...
                (copied from incoming/)

3. ShakeMap  →  work/<event_id>/current/products/
                (ShakeMap writes output here)

4. Publish   →  products/<event_id>/
                (atomically copied from work/)
```

The original files in `incoming/` are preserved — they are copied (not moved) to `work/`.

---

## ShakeMap Profile Structure

ShakeMap uses a profile-based configuration system located in the container user's home directory:

```
/home/sysop/
├── .shakemap/
│   ├── profiles.conf                 Profile registry
│   └── .shakemap_readiness_status    Readiness sentinel file
└── shakemap_profiles/
    └── <profile_name>/
        ├── install/
        │   ├── config/               Configuration files
        │   │   ├── model.conf        Ground motion model settings
        │   │   ├── select.conf       GMPE selection rules
        │   │   ├── modules.conf      Module configuration
        │   │   ├── products.conf     Output product settings
        │   │   └── ...
        │   ├── data/
        │   │   └── layers/           Region boundary polygons (.wkt)
        │   └── logs/                 ShakeMap processing logs
        └── data → SERVICE_ROOT/work  Symlink (created by configure-shakemap.sh)
```

The `data` symlink is key to the integration. ShakeMap expects input files at `<profile>/data/<event_id>/current/`. Because `data` points to `SERVICE_ROOT/work`, when the service copies files to `work/<event_id>/current/`, ShakeMap finds them in the expected location.

---

## Volume Mount Recommendations

### What to Mount

Mount the `RUNTIME_ROOT` directory (`/home/sysop/runtime` by default) to persist all service data:

```bash
docker run -v ./runtime:/home/sysop/runtime ...
```

Or use the start script:

```bash
./scripts/start-shakemap-docker.sh --runtime ./runtime
```

This single mount covers all six service directories plus shared data.

### What Survives Container Restart

| Item | Location | Survives Restart? |
|------|----------|-------------------|
| Event data (incoming, products, status) | Under `SERVICE_ROOT/` | **Yes** — on mounted volume |
| VS30 and topo grids | `SERVICE_ROOT/data/` | **Yes** — on mounted volume |
| ShakeMap profile configuration | `~/shakemap_profiles/` | **No** — inside container filesystem |
| Readiness sentinel | `~/.shakemap/` | **No** — inside container filesystem |
| `profiles.conf` | `~/.shakemap/profiles.conf` | **No** — inside container filesystem |

Because the ShakeMap profile and readiness sentinel are inside the container filesystem (not on the mounted volume), you must re-run `configure-shakemap.sh` after every container recreation. The configure script is idempotent and fast when data files already exist on the mounted volume.

### What NOT to Mount

Do not mount individual subdirectories (e.g., only `events/` or only `products/`). The service expects all six directories to share the same parent filesystem for atomic rename operations.

---

## File Ownership

All directories are created by the `sysop` user (UID 1000, GID 1000). On Linux, the host directory used for the volume mount must be writable by this user.

See the [Permissions Guide](permissions.md) for platform-specific details and troubleshooting.

---

## Related Documentation

- [Quick Start](quick-start.md) — getting started
- [Execution Workflow](execution-workflow.md) — how files move through the system
- [Permissions Guide](permissions.md) — file ownership and platform behavior
