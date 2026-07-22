# Runtime layout and persistence

```text
runtime/
└── shakemap/
    ├── incoming/
    ├── products/
    ├── logs/
    ├── data/
    │   ├── vs30/global_vs30.grd
    │   ├── topo/topo_30sec.grd
    │   ├── global/strec/
    │   └── test/v4.4.9/
    └── .service/
        ├── events/
        ├── work/
        ├── archive/
        └── preparation/
            ├── manifest.json
            ├── report.md
            ├── base/global/
            ├── attempts/
            ├── logs/
            └── history/
```

Only `incoming/`, `products/`, `logs/`, and `data/` are user-facing top-level
directories. Internal event, work, archive, and preparation state lives below
`.service/`. New work must not create top-level `events/`, `work/`, or
`archive/`.

The entire `runtime/` directory is mounted at `/home/sysop/runtime`. The nested
`runtime/shakemap/data/` mount is read-only. Preparation output, profiles
templates, reports, logs, and both native verification runs therefore survive
container recreation; no required preparation state exists only under
`/home/sysop` in the container layer.
