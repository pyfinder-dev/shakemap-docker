# Quick start

```bash
source /path/to/project/.venv/bin/activate
python -m pip install -e .
./scripts/build-shakemap-docker.sh
./scripts/manage-shakemap-data.sh inspect
./scripts/start-shakemap-docker.sh
curl -fsS http://localhost:9010/config
curl -fsS http://localhost:9010/healthz
```

The image helper reads the fixed release from `VERSIONS.env`, verifies an
untagged candidate, and changes `shakemap-docker:latest` only after the
candidate passes. It does not install the project into the host environment.
Image success is not deployment readiness.

For full pinned checksum validation:

```bash
./scripts/manage-shakemap-data.sh validate
```

For isolated service state with an existing exact data tree:

```bash
./scripts/start-shakemap-docker.sh \
  --name shakemap-isolated \
  --runtime /path/to/disposable-runtime \
  --data /path/to/existing/shakemap/data \
  --port 19010
```

The data tree is mounted read-only. The APIs will report liveness and data
evidence, but overall readiness remains false because managed execution is
disabled.

Keep the project Python environment active when running host helpers.
