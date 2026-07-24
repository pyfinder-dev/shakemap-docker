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

The existing `shakemap_service.preparation` module exposes one narrow,
read-only inventory for the disabled API. That inventory is independent of the
module's retained legacy preparation lifecycle, which startup does not invoke.

The retained legacy module still contains preparation-container, base-snapshot,
readiness-sentinel, and `.service/preparation` behavior. That obsolete
lifecycle is not used by startup or inventory; its removal belongs to the
separate Phase 0B pass.
