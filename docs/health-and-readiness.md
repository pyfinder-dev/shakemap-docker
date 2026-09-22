# Health and readiness

`GET /healthz` and `shake-in-docker health` return the same lightweight view:

```json
{"ready": false, "reason": "operational explanation", "shakemap_version": "4.4.9"}
```

A responsive service returns HTTP 200 even when `ready=false`. A ready service
returns `reason=null`. HTTP availability alone does not permit submissions:
`POST /events` returns HTTP 503 while the service is not ready.

Readiness is recorded by successful finalization and checked against the
current deployment identity. Missing, invalid, failed, or mismatching readiness
records cannot authorize calculation admission. Normal startup does not rerun
finalization or silently repair a mismatch.

Finalization uses an isolated, network-disabled service for its verification
calculation before publishing durable readiness. Its temporary admission state
does not count as successful finalization. Failures revoke readiness and stop
the service while retaining diagnostic evidence.

Health and configuration queries do not hash large grids, run ShakeMap, or
evaluate scientific coverage. Data validation proves the scope reported by the
data helper; a successful calculation proves the recorded inputs, selected
configuration, module sequence, and product gates for that calculation only.

Missing assets in an isolated runtime describe only that isolated runtime.
Operators must inspect the exact mounted data tree before concluding that
datasets are missing.
