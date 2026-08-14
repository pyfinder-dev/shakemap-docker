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

The `shakemap_service.preparation` module exposes the narrow read-only
inventory used by the disabled API, explicit pinned global-data
validation/provisioning, and retained native-product readers. It does not
create or depend on persistent preparation state or a retained verification
workspace.
