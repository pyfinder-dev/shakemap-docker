# Current execution workflow

The supported current workflow is:

1. Build and verify the immutable image.
2. Provision or validate global data in the selected runtime.
3. Finalize the canonical deployment, including its fixed global calculation.
4. Verify the running service through REST and `shake-in-docker`.
5. Submit caller-prepared native files and poll the matching calculation.

Managed execution is implemented; public submission requires recorded readiness.
The caller supplies `event_id`, native input files including `event.xml`, and
optionally `configuration` (default `global`) and `overwrite` (default `true`).
The service snapshots inputs, assigns a correlation sequence, and queues work.
Same-ID submissions are serialized; different IDs may run within configured
capacity. The service does not infer earthquake grouping or submission cadence.

The native sequence is `select assemble model contour mapping stations gridxml`.
Configuration materialization or execution failure fails the calculation;
there is no fallback to another configuration. Success requires validated core
products, provenance, a product manifest, and both required logs. Poll event
detail for the accepted `internal_sequence`; require `SUCCESS`,
`job_completed=true`, and `products_ready=true` before collecting products.

The implemented workflow still needs completed running-service evidence; see
[current verification status](../README.md#current-verification-status).

Native verification of a fixed package, where explicitly invoked, proves only
that scenario, release, inputs, module plan, and validation gate. It does not
establish REST submission, queue behavior, recalculation archival, concurrency,
or production readiness.
