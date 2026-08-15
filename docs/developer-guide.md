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

For running checks, test both an empty isolated runtime and a disposable
service-state runtime mounted to the exact existing project data tree. Use
`start-shakemap-docker.sh --data`; do not use a manual `docker run` substitute.

Before and after checks against operator data, compare metadata-only evidence
such as path, size, and modification time. Do not hash hundreds of megabytes
merely to prove non-mutation.

Do not infer scientific readiness from uniform VS30, file presence, or partial
products. Managed readiness remains false until the contracted configuration,
execution, provenance, manifest, log, and core-product gates are implemented.
