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
│   └── test/<resolved-version>/
└── .service/
    ├── events/
    └── archive/
```

The user-facing top level is exactly `products/`, `logs/`, and `data/`.
Service-owned state is under `.service/events/` and `.service/archive/`.

New runs do not use `incoming/`, `.service/work/`, `.service/preparation/`, or
top-level `events/`, `work/`, `archive/`, or `preparation/`.

`products/<event_id>/` belongs to ShakeMap and contains only its native event
tree. Service status, requests, effective configuration, provenance, manifests,
private home/profile material, and service logs belong under
`.service/events/<event_id>/`. These event execution responsibilities are
contracted but not implemented by the current disabled managed-execution path.
