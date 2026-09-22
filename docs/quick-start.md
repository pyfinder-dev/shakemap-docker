# Quick start

```bash
source /path/to/project/.venv/bin/activate
python -m pip install -e .
make build
make data RUNTIME_ROOT=./runtime
make finalize RUNTIME_ROOT=./runtime PORT=9010 MAX_CONCURRENT=10
make verify RUNTIME_ROOT=./runtime PORT=9010 MAX_CONCURRENT=10
shake-in-docker health
shake-in-docker configurations
```

The image helper reads the fixed release from `VERSIONS.env`, verifies an
untagged candidate, and changes `shakemap-docker:latest` only after the
candidate passes. It does not install the project into the host environment.
Image success is not deployment readiness.

For full pinned checksum validation:

```bash
./scripts/manage-shakemap-data.sh validate
```

Finalization refuses queued/running work, prepares the runtime and mounts,
checks effective write access, and runs the fixed South Napa/global calculation.
Successful finalization leaves the canonical service running. A failure must be
resolved before use; follow the diagnostic and the
[permission recovery instructions](permissions.md) when applicable.

`make verify` requires an already-running canonical deployment; it includes a
new run of the fixed verification calculation. It does not build or start the
service. See the [current verification status](../README.md#current-verification-status)
for which execution gates have actually passed.

After successful finalization, submit native inputs through the host client:

```bash
shake-in-docker submit example --configuration global --overwrite true \
  --file /path/to/event.xml --file /path/to/event_dat.xml
shake-in-docker status example
shake-in-docker products example
shake-in-docker queue
```

Select the complete runtime using `RUNTIME_ROOT`; only its scientific-data
subtrees are read-only overlays. Use `shake-in-docker --url http://localhost:9010 ...`
for an explicit service URL. Use `make stop` for graceful shutdown and
`make start` for a matching, already-finalized deployment. Changing the image
or runtime requires finalization. Keep the project environment active; do not
run finalization with `sudo`.
