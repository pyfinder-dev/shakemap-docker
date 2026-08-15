# Quick start

```bash
./scripts/build-shakemap-docker.sh
./scripts/manage-shakemap-data.sh inspect
./scripts/start-shakemap-docker.sh
curl -fsS http://localhost:9010/config
curl -fsS http://localhost:9010/healthz
```

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

Activate the project Python environment before running host data helpers.
