# Health and preparation reporting

`GET /config` and `GET /healthz` load the mounted
`.service/preparation/manifest.json`. They compare its ShakeMap and immutable
support identity with the running image and verify that the base snapshot
exists.

- `scientific_readiness.ready: true` means the current image identity matches a
  durable preparation record whose fixed California and prepared-global native
  plans passed offline.
- `scientific_readiness.ready: false` means preparation is absent, invalid,
  incomplete, missing its base snapshot, or belongs to another image.
- `/healthz` may run in `not_ready` state; startup does not invent a hard refusal.

The response includes the manifest path, preparation record, global base path,
external grid paths, image identity, proof scope, and explicit non-claims. It
does not report a container-home sentinel, shared active profile, or uniform
VS30 override.

Preparation readiness is bounded integration evidence. It is not authoritative
calculation `SUCCESS`, queue correctness, REST submission proof, regional
readiness, concurrency proof, or universal geographic/scientific validity.
