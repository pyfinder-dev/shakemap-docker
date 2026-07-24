# Architecture

The system has four deliberately separate concerns:

1. The image build resolves one official stable ShakeMap release and records
   immutable identity and generic mapping/STREC support.
2. External scientific data is operator-owned below `shakemap/data/`.
   Inspection is read-only; provisioning is explicit.
3. The running FastAPI process reports identity, configuration discovery,
   data evidence, liveness, and disabled managed readiness.
4. Managed calculation execution remains disabled until effective
   configuration resolution and the full calculation success contract exist.

`shakemap_service.data_assets` performs cheap read-only inspection.
`shakemap_service.data_management` performs explicit provisioning and full
pinned validation. Neither owns persistent service lifecycle state.

There is no preparation container, base snapshot, readiness sentinel, or
`.service/preparation` tree. Disposable verification workspaces may be used by
verification commands, but they are evidence for the tested scenario only and
do not become service readiness state.
