# Runtime Layout Guide

This guide documents the complete directory structure inside the ShakeMap Docker container, what each directory contains, and how to set up volume mounts for data persistence.

For the top-level overview, see the [Runtime Directory Layout](../README.md#runtime-directory-layout) section in the README.

---

## Service Directory Tree

The service root at `SERVICE_ROOT` (default: `/home/sysop/runtime/shakemap/`) contains four user-facing directories plus a hidden internal service state directory:

```
/home/sysop/runtime/shakemap/         (SERVICE_ROOT)
├── incoming/                         Staged input files
│   └── <event_id>/
│       ├── event.xml
│       ├── event_dat.xml
│       └── rupture.json
├── products/                         Published outputs
│   └── <event_id>/
│       ├── grid.xml                  ShakeMap output
│       ├── intensity.jpg
│       ├── ...
│       ├── products-manifest.json    Product inventory + validation
│       └── service-record/           Audit copy
│           ├── requeststatus.json    Status snapshot at publication
│           └── provenance.json       Input hashes, versions, modules
├── logs/                             Operator troubleshooting
│   └── <event_id>.log               ShakeMap stdout/stderr capture
├── data/                             Shared data files
│   ├── vs30/
│   │   └── global_vs30.grd          VS30 grid (downloaded or mounted)
│   └── topo/
│       └── topo_30sec.grd           Topography grid
└── .service/                         Internal service state (hidden)
    ├── events/                       Event tracking
    │   └── <event_id>/
    │       ├── requeststatus.json    Authoritative event status
    │       └── provenance.json       Execution provenance record
    ├── work/                         ShakeMap processing (private)
    │   └── <event_id>/
    │       └── current/
    │           ├── event.xml         Copied from incoming/
    │           └── products/         ShakeMap output (before publication)
    └── archive/                      Completed-run archive (future)
```

### Directory Purposes

| Directory | Contents | Visibility |
|-----------|----------|------------|
| `incoming/` | Staged input files for each submitted event. Files are written atomically (temp dir → rename) so consumers never see partial data. | **User-facing** |
| `products/` | Published ShakeMap outputs. Products are atomically copied from the work area after validation. Includes `products-manifest.json` and `service-record/` audit copy. | **User-facing** |
| `logs/` | Per-event execution logs (`<event_id>.log`). Contains ShakeMap CLI stdout/stderr capture for each processed event. | **User-facing** |
| `data/` | Shared data files (VS30 grids, topography grids) downloaded or mounted during Stage 2 configuration. | **User-facing** |
| `.service/events/` | Per-event tracking directories. Each event has `requeststatus.json` (authoritative lifecycle record) and `provenance.json` (execution provenance). | **Internal** |
| `.service/work/` | ShakeMap's private processing directory. Input files are copied here from `incoming/` before execution. Also the target of the ShakeMap profile `data` symlink. | **Internal** |
| `.service/archive/` | Archive storage for completed runs. Reserved for future use. | **Internal** |

---

## Per-Event File Lifecycle

When an event is submitted and processed, files move through the directory structure:

```
1. Submit    →  incoming/<event_id>/event.xml, event_dat.xml, ...
                .service/events/<event_id>/requeststatus.json

2. Execute   →  .service/work/<event_id>/current/event.xml, ...
                (copied from incoming/)

3. ShakeMap  →  .service/work/<event_id>/current/products/
                (ShakeMap writes output here)

4. Validate  →  Check required core products (grid.xml or shake_result.hdf)

5. Publish   →  products/<event_id>/
                (atomically copied from work/)
                products/<event_id>/products-manifest.json
                products/<event_id>/service-record/

6. Log       →  logs/<event_id>.log
                (ShakeMap stdout/stderr captured during execution)
```

The original files in `incoming/` are preserved — they are copied (not moved) to `.service/work/`.

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
        └── data → SERVICE_ROOT/.service/work  Symlink (created by configure-shakemap.sh)
```

The `data` symlink is key to the integration. ShakeMap expects input files at `<profile>/data/<event_id>/current/`. Because `data` points to `SERVICE_ROOT/.service/work`, when the service copies files to `.service/work/<event_id>/current/`, ShakeMap finds them in the expected location.

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

This single mount covers all service directories plus shared data.

### What Survives Container Restart

| Item | Location | Survives Restart? |
|------|----------|-------------------|
| Event data (incoming, products, status) | Under `SERVICE_ROOT/` | **Yes** — on mounted volume |
| Event execution logs | `SERVICE_ROOT/logs/` | **Yes** — on mounted volume |
| VS30 and topo grids | `SERVICE_ROOT/data/` | **Yes** — on mounted volume |
| ShakeMap profile configuration | `~/shakemap_profiles/` | **No** — inside container filesystem |
| Readiness sentinel | `~/.shakemap/` | **No** — inside container filesystem |
| `profiles.conf` | `~/.shakemap/profiles.conf` | **No** — inside container filesystem |

Because the ShakeMap profile and readiness sentinel are inside the container filesystem (not on the mounted volume), you must re-run `configure-shakemap.sh` after every container recreation. The configure script is idempotent and fast when data files already exist on the mounted volume.

### What NOT to Mount

Do not mount individual subdirectories (e.g., only `.service/events/` or only `products/`). The service expects all directories to share the same parent filesystem for atomic rename operations.

---

## File Ownership

All directories are created by the `sysop` user (UID 1000, GID 1000). On Linux, the host directory used for the volume mount must be writable by this user.

See the [Permissions Guide](permissions.md) for platform-specific details and troubleshooting.

---

## Related Documentation

- [Quick Start](quick-start.md) — getting started
- [Execution Workflow](execution-workflow.md) — how files move through the system
- [Permissions Guide](permissions.md) — file ownership and platform behavior
