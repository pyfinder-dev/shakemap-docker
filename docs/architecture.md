# Architecture

The system has four deliberately separate concerns:

1. The image build resolves one official stable ShakeMap release and records
   immutable identity and generic mapping/STREC support.
2. External scientific data is operator-owned below `shakemap/data/`. Stage 2
   manages only global VS30 and topography. Inspection and validation are
   read-only; provisioning installs only a missing asset.
3. The FastAPI service exposes identity, configuration names, readiness,
   submission, event status, current products, and queue state. The host CLI
   calls the same REST interface.
4. Managed execution snapshots native inputs, serializes same-ID work,
   materializes the selected configuration, runs the fixed native module plan,
   and publishes success only after product and evidence validation.

The `shakemap_service.preparation` module owns data-helper inspection,
integrity validation, missing-only provisioning, and staging. Finalization
owns staged-data activation, runtime preparation, effective write-access checks,
and the verification handshake. Normal startup requires matching recorded
readiness and does not repeat finalization.

Scientific-data read repair and service-writable ownership repair have separate
helpers because they operate on disjoint trees with different permitted changes.
Automatic writable repair and its manual privileged command share one
implementation. See [permissions](permissions.md) and the
[current verification status](../README.md#current-verification-status).
