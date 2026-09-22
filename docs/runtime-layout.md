# Runtime layout

```text
runtime/shakemap/
├── products/
├── logs/
├── data/
│   ├── global/
│   │   ├── vs30/
│   │   ├── topo/
│   │   └── strec/slabs/
│   ├── regional/
│   ├── test/<resolved-version>/
│   └── inputs/<event_id>/
└── .service/
    ├── events/
    ├── archive/
    ├── queue/
    ├── readiness.json
    └── workflow.lock
```

The user-facing top level is exactly `products/`, `logs/`, and `data/`.
Service-owned state is under `.service/events/` and `.service/archive/`.

New runs do not use `incoming/`, `.service/work/`, `.service/preparation/`, or
top-level `events/`, `work/`, `archive/`, or `preparation/`.

`products/<event_id>/` belongs to ShakeMap and contains only its native event
tree. Service status, requests, effective configuration, provenance, manifests,
private home/profile material, and service logs belong under
`.service/events/<event_id>/`. Native output is written directly under
`products/<event_id>/current/`.

The runtime mounts at `/home/sysop/runtime`; `data/global/`, `data/regional/`,
and `data/test/` have read-only overlays. `data/inputs/` and the three
service-owned trees (`products/`, `logs/`, `.service/`) remain writable.
Public artifact paths use the configured host runtime root.

On a same-ID recalculation, `overwrite=true` discards both preceding trees and
starts clean. `overwrite=false` retains them under
`.service/archive/<event_id>-<UTC timestamp>/products/` and `service/` before
starting clean. Concurrent work with the same caller ID is serialized.
