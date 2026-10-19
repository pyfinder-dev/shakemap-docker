# Developer guide

Activate the project Python environment before running project scripts and
tests. Install the package into that environment so the helpers can use the
`shake-in-docker` host client. Do not use the system Python for project work.

Prefer an installation without Conda inside the running Docker container.
Consider Conda only if it demonstrably makes installation and ongoing
maintenance substantially simpler; upstream ShakeMap owns scientific dependency
requirements.

The ShakeMap 4.5 build resolves the selected release's `environment.yml` with
Micromamba in a separate build stage. A pip-only build satisfied Python package
metadata but installed h5py with HDF5 2.0.0, outside upstream's HDF5 2.1.x
requirement. Copying the resolved environment into the final image avoids
maintaining separate native builds and keeps package-manager executables out of
the running container. Wrapper installation must preserve the resolved
scientific packages. Installed versions, source-environment provenance and
native-library checks are retained with image identity. This is a native solve
of upstream requirements, not exact reproduction of its platform-specific lock.
On Linux ARM64, the upstream `esi-core` requirement is installed through PyPI
because its Conda-forge package is unavailable for that platform. The version
requirement is copied unchanged from upstream and the provider adjustment is
recorded in image identity.

Install the host development requirements before running tests or `make verify`.
This includes the editable project installation and the test dependencies:

```bash
python -m pip install -r requirements.txt
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
host tests do not prove real image or deployment behavior. Run and inspect each
[verification level](../README.md#verification-and-outcome-evidence) separately.

Comment helper responsibilities, preconditions, and non-obvious behavior;
avoid historical or milestone commentary in code. Keep each workflow in its
responsible helper and Make recipes as thin aliases. Finalization and manual
writable-path recovery reuse one repair implementation. Privileged recovery
is explicitly operator-invoked, never automatic `sudo`.
