# Runtime preparation and data

## External locations

| Asset | Host path |
|---|---|
| Global VS30 | `runtime/shakemap/data/vs30/global_vs30.grd` |
| Global topography | `runtime/shakemap/data/topo/topo_30sec.grd` |
| Slab2 grids | `runtime/shakemap/data/global/strec/slabs/` |
| Slab2 provenance | `runtime/shakemap/data/global/strec/slab2-manifest.json` |
| California package | `runtime/shakemap/data/test/v4.4.9/` |
| Global base snapshot | `runtime/shakemap/.service/preparation/base/global/` |

The large global grids retain their established paths. Preparation does not
move or duplicate them to satisfy a cosmetic layout change.

## Validation

Host checks require HDF5/netCDF4 signature, exact byte size, and a pinned
SHA-256. Native checks open the grids with the resolved image's raster stack,
verify exact bounds, and confirm the fixed scenario is covered. The USGS
directory indexes publish filenames and sizes but not checksums; manifests
therefore distinguish project-verified download pins from source-published
checksums.

Slab2 is external because the resolved `select` module configures STREC with
both GCMT and slab support. GCMT is already the installed STREC
`moment_tensors.db` in the image. The helper verifies the pinned Slab2 archive,
extracts it atomically, inventories every file, and opens all 108 grids with the
image's native raster reader.

## Safe replacement

Valid files are never rewritten. A candidate download/import is completed and
validated in a unique sibling path first. If the destination is explicitly
invalid, it is preserved with an `invalid-<timestamp>` name immediately before
atomic placement of the valid candidate.

The generated global base uses absolute mounted scientific-data paths. It is a
validated template snapshot, not one active mutable profile. Shared data are
mounted read-only in both preparation verification and the service container.
