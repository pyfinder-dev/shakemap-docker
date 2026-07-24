# Health and readiness

`GET /healthz` response schema `"1.0"` separates:

- `process_liveness`: the HTTP process responded;
- `infrastructure`: contracted paths and ShakeMap CLI PATH presence;
- `data`: cheap external-data presence/readability and manifest evidence;
- `managed_execution_readiness`: always false in the current implementation;
- `overall_readiness`: therefore false.

The data inspection does not hash large grids, open scientific datasets, invoke
native ShakeMap, or evaluate event coverage. Those states are reported as
`not_evaluated`. A release-matched package may be `compatible` only on the basis
of its small manifest matching immutable image identity; that does not validate
the package payload.

Missing assets in an isolated runtime describe only that isolated runtime.
Operators must inspect the exact mounted data tree before concluding that
datasets are missing.
