# Public preparation reporting

## `GET /config`

Returns immutable image identity, durable preparation state, the global base
snapshot, external VS30/topography paths, configured module plan, proof scope,
and explicit non-claims.

```bash
curl -fsS http://localhost:9010/config | python -m json.tool
```

## `GET /healthz`

Returns infrastructure health plus the same preparation identity and readiness
decision. A missing or mismatched manifest produces `not_ready` and an action
that points to the host preparation command. The API is allowed to run while
not ready.

```bash
curl -fsS http://localhost:9010/healthz | python -m json.tool
```

## `GET /config/profiles`

Reports the mounted global base snapshot and states that a shared mutable
active profile is unsupported.

## Calculation endpoints

Calculation routes currently exist in the codebase, but this correction does
not establish their final input, queue, execution, archival, CLI-parity, or
`SUCCESS` semantics. They are not documented here as production-ready public
capabilities. Preparation readiness must not be interpreted as a managed
calculation guarantee.
