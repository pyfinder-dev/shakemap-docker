# Developer guide

Use the project environment for all project Python work:

```bash
source /Users/savas/my-codes/eew/pyfinder-dev/.venv/bin/activate
```

Run host tests:

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

Verification is separated into:

1. host-side tests;
2. container-internal image/module checks;
3. running-service deployment checks.

Use only the canonical image `shakemap-docker:latest` and container
`shakemap-docker`. Inspect existing mounts before replacing the container and
preserve operator runtime data. Run scenarios sequentially. `make finalize`
prepares the deployment; `make verify` requires it to be running. Neither
supports a separate `--data` mount override. An empty test runtime cannot prove
anything about operator datasets in the actual mounted runtime.

Before and after checks against operator data, compare metadata-only evidence
such as path, size, and modification time. Do not hash hundreds of megabytes
merely to prove non-mutation.

Do not infer scientific readiness from uniform VS30, file presence, or partial
products. The calculation and readiness gates are implemented, but successful
host tests do not prove real image or deployment behavior. Check the
[current verification status](../README.md#current-verification-status).

Comment helper responsibilities, preconditions, and non-obvious behavior;
avoid historical or milestone commentary in code. Keep each workflow in its
responsible helper and Make recipes as thin aliases. Finalization and manual
writable-path recovery reuse one repair implementation. Privileged recovery
is explicitly operator-invoked, never automatic `sudo`.
