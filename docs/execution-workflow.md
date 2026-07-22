# Execution boundary

This corrective release establishes build, runtime preparation, native
integration checks, and preparation reporting. Its supported sequence is:

```text
build → prepare/validate → start → inspect /config and /healthz
```

Preparation executes two fixed native scenarios directly in a short-lived,
network-disabled container. Those executions are integration evidence and do
not enter the service queue or set service calculation status.

The existing calculation routes and worker code are not proof of the future
contract. Durable FIFO redesign, structured-origin and prediction-only input,
regional/private profile execution, concurrency, recalculation archival,
public `shake-docker`, and authoritative product-gated `SUCCESS` remain later
work. Do not describe a preparation-native exit code or product inventory as a
successful managed calculation.
