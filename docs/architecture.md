# Architecture

```text
build immutable image
        │
        ▼
host validates/provisions external data
        │
        ▼
network-disabled short-lived preparation container
  ├─ verifies UID/GID and read-only scientific mounts
  ├─ creates release-native templates
  ├─ writes mounted global base snapshot
  └─ runs fixed California and prepared-global checks
        │
        ▼
stable service container starts and reports mounted preparation identity
```

The immutable image owns generic support: Natural Earth mapping files and the
installed STREC moment database. The mounted runtime owns large scientific
data, Slab2, generated configuration, reports, logs, and service state.

`shakemap_service.preparation` implements both host orchestration and the
container-internal preparation operation. `configure-shakemap.sh` is the single
operator-facing wrapper. `build_identity` records immutable image support, and
`main` exposes the current image/preparation relationship through `/config` and
`/healthz`.

The global base is a template snapshot. It is not switched or modified by
event execution. Private per-calculation materialization is a later capability
and is not implemented by this correction.
