# Configuration and external data

The caller-selected configuration contract defaults omission to `global`.
Effective configuration resolution is not implemented, so the service lists
discovered names and validation state but does not run calculations.

| Data class | Contracted path |
|---|---|
| Global VS30 | `runtime/shakemap/data/global/vs30/global_vs30.grd` |
| Global topography | `runtime/shakemap/data/global/topo/topo_30sec.grd` |
| STREC slabs | `runtime/shakemap/data/global/strec/slabs/` |
| Slab manifest | `runtime/shakemap/data/global/strec/slab2-manifest.json` |
| Regional data/configurations | `runtime/shakemap/data/regional/<name>/` |
| Verification package | `runtime/shakemap/data/test/<resolved-version>/` |

Use `manage-shakemap-data.sh inspect` for cheap filesystem evidence and
`manage-shakemap-data.sh validate` for full pinned global-asset checks. Manual
placement is supported. The `provision` action may explicitly import or
download global assets; it is never run by container startup or API inspection.

Uniform VS30 is not readiness evidence and is not used by these checks.
