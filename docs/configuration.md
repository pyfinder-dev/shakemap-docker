# Configuration and external data

The caller selects the configuration through REST or `--configuration NAME`.
Omission means `global`. Selection is never inferred from event geography.
Missing configurations, materialization failures, and native execution failures
fail the calculation without falling back to `global` or another name.
`shake-in-docker configurations` lists available names; listing a name does
not validate that configuration or its scientific data.

| Data class | Contracted path |
|---|---|
| Global VS30 | `runtime/shakemap/data/global/vs30/global_vs30.grd` |
| Global topography | `runtime/shakemap/data/global/topo/topo_30sec.grd` |
| Regional data/configurations | `runtime/shakemap/data/regional/<name>/` |
| Verification package | `runtime/shakemap/data/test/<resolved-version>/` |

Use `manage-shakemap-data.sh inspect` for cheap filesystem evidence and
`manage-shakemap-data.sh validate` for full pinned global-asset checks. Manual
placement is supported. The `provision` action may explicitly import or
download only a missing global VS30 or topography asset; it is never run by
container startup or API inspection. Valid existing assets are reused. Invalid,
incomplete, or unexpected existing assets fail validation and remain unchanged.
Stage 2 does not manage small image-resident or finalization support data.

Uniform VS30 is not readiness evidence and is not used by these checks.

Regional presets are seeded only when missing. Existing operator configurations
are preserved. The Italy preset still needs separate data/path validation
before use; successful global calculations would not establish Italy coverage.
Any future synthetic showcase package must be isolated, explicitly labelled,
and accompanied by provenance and checksums. It cannot support scientific or
general regional readiness claims.
