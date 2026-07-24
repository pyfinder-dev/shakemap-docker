# Read-only configuration and health API

## `GET /config`

Returns response schema `"1.0"`, immutable/deployment identity, the contracted
data root, per-asset inspection states, discovered configuration names, the
default `global` name, and the explicit unimplemented configuration-resolution
boundary.

## `GET /config/profiles`

Lists the same discovered configuration names for compatibility with the
current route. It does not claim an active mutable profile or validated
configuration.

## `GET /healthz`

Returns liveness and infrastructure separately from external-data evidence,
managed-execution readiness, and overall readiness.

The API does not perform full checksums or scientific validation. Use
`scripts/manage-shakemap-data.sh validate` for explicit full pinned global
asset validation.

Submission remains unavailable with HTTP 503 while managed execution is
disabled. Queue, recalculation, archive, configuration selection, and managed
calculation behavior are outside this corrective pass.
