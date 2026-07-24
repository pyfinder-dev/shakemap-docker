# Current execution workflow

The supported current workflow is:

1. Build and verify the immutable image.
2. Inspect the exact external data tree.
3. Optionally run explicit full checksum validation.
4. Start the service with state and data mounted separately.
5. Inspect `/config` and `/healthz`.

Managed calculations are not part of this workflow. The public submission route
returns HTTP 503 because effective configuration resolution and authoritative
success semantics are not implemented.

Native verification of a fixed package, where explicitly invoked, proves only
that scenario, release, inputs, module plan, and validation gate. It does not
establish REST submission, queue behavior, recalculation archival, concurrency,
or production readiness.
