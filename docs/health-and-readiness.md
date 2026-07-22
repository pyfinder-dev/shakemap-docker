# Health and preparation reporting

`GET /config` and `GET /healthz` load the mounted
`.service/preparation/manifest.json`. They compare its ShakeMap and immutable
support identity with the running image and verify that the base snapshot
exists.

- `process_liveness.live: true` means the HTTP process is responding. It does
  not assert preparation or calculation readiness.
- `preparation_readiness.ready: true` means the current image identity matches
  a durable preparation record whose fixed California and prepared-global
  native plans and product-structure gates passed offline.
- `preparation_readiness.ready: false` means preparation is absent, invalid,
  incomplete, missing its base snapshot, or belongs to another image.
- `managed_calculation_readiness.ready` remains `false` while managed execution
  is disabled and submission returns HTTP 503.
- `overall_readiness.ready` therefore remains `false`, even when preparation
  passed. `/healthz` returns liveness without calling the calculation service
  healthy or ready.

The response includes the manifest path, preparation record, global base path,
external grid paths, image identity, proof scope, and explicit non-claims. It
does not report a container-home sentinel, shared active profile, or uniform
VS30 override.

Preparation readiness is bounded integration evidence. It is not authoritative
calculation `SUCCESS`, queue correctness, REST submission proof, regional
readiness, concurrency proof, or universal geographic/scientific validity.
