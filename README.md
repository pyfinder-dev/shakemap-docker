# ShakeMap Docker

This project builds a reproducible USGS ShakeMap image and prepares external
scientific data on a mounted host runtime before the long-running API container
is created.

## Normal workflow

```bash
source ../.venv/bin/activate
./scripts/build-shakemap-docker.sh
./scripts/configure-shakemap.sh
./scripts/start-shakemap-docker.sh
curl -fsS http://localhost:9010/config | python -m json.tool
curl -fsS http://localhost:9010/healthz | python -m json.tool
```

Normal resources are image `shakemap-docker:latest`, container
`shakemap-docker`, runtime `./runtime`, and host port `9010`. Configuration is
not performed through the stable container. If that container already exists,
the start helper preserves it and explains the operator's choices.

## Image and external data boundary

The image contains one resolved official stable ShakeMap release; installed
package identities; the `moment_tensors.db` shipped by STREC, exposed through a
read-only symlink; and four checksum-pinned Natural Earth 10 m layers required
by default mapping.

The mounted runtime contains:

- `shakemap/data/vs30/global_vs30.grd`;
- `shakemap/data/topo/topo_30sec.grd`;
- `shakemap/data/global/strec/slabs/` and its provenance manifest;
- the small California package under `data/test/v4.4.9/`;
- the global base snapshot and preparation evidence under
  `shakemap/.service/preparation/`;
- all service state, logs, tests, and generated output.

The service mounts `shakemap/data/` read-only. Startup never downloads or
mutates scientific inputs.

## Preparation behavior

`configure-shakemap.sh` validates existing global grids by file type, exact
size, pinned project digest, native raster readability, geographic bounds, and
fixed-scenario coverage. Valid existing 582 MB and 238 MB files are reused in
place without download or cosmetic relocation.

Missing or explicitly invalid inputs are obtained from official USGS URLs.
Downloads use unique sibling temporary files and atomic placement. An invalid
existing file is preserved with an `invalid-<timestamp>` suffix after a valid
replacement is ready. USGS publishes these files without adjacent checksums;
the recorded SHA-256 values are project-verified pins, not source-published
checksums.

Manual placement is supported:

```bash
./scripts/configure-shakemap.sh \
  --vs30-source /data/global_vs30.grd \
  --topo-source /data/topo_30sec.grd \
  --slab-source /data/slab2.zip \
  --no-download
```

The helper uses a short-lived, network-disabled container from the built image
to generate release-native templates with `sm_profile` and `shake init`,
correct the stale release VS30 default, validate schemas and expanded paths,
check UID/GID permissions, and retain a base snapshot. This is not a mutable
active profile. Later calculations must receive private copies.

## Evidence and proof boundary

Preparation runs the complete plan
`select assemble model contour mapping stations gridxml` twice without network:
once with the California package and once with prepared global data. Exact
commands, configuration/data identities, stdout, stderr, module order, and
output inventories are retained under:

- `runtime/shakemap/.service/preparation/manifest.json`
- `runtime/shakemap/.service/preparation/report.md`
- `runtime/shakemap/.service/preparation/base/global/`
- `runtime/shakemap/.service/preparation/logs/`

These checks prove image/preparation integration for two fixed scenarios. They
do not prove queue correctness, REST submission, structured-origin or
prediction-only support, named regions, concurrency, recalculation archival,
authoritative service `SUCCESS`, or universal scientific validity.

## Runtime layout

```text
runtime/shakemap/
├── incoming/
├── products/
├── logs/
├── data/
└── .service/
    ├── events/
    ├── work/
    ├── archive/
    └── preparation/
```

New runs never create top-level `events/`, `work/`, or `archive/`. Container
recreation preserves preparation because required state is mounted.

## Verification levels

- Host tests cover reuse, manual import, corrupt/partial data, atomic placement,
  manifests, permissions, package migration, and script/document references.
- `verify-shakemap-image.sh` checks image identity, Natural Earth, the installed
  STREC database, Cartopy offline resolution, imports, and modules.
- Preparation retains both fixed offline native executions.
- `verify-shakemap-deployment.sh` checks `/config` and `/healthz` through an
  isolated running service.

No one layer alone proves deployment or scientific readiness. See
[quick start](docs/quick-start.md), [configuration](docs/configuration.md),
[runtime layout](docs/runtime-layout.md), [health](docs/health-and-readiness.md),
and [troubleshooting](docs/troubleshooting.md).
