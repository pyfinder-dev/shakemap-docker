# Troubleshooting

## An asset is reported absent

Confirm that the service mounted the intended exact tree:

```bash
./scripts/manage-shakemap-data.sh inspect --runtime /path/to/runtime
```

An empty disposable runtime proves only that its own data tree is empty. Use
the same `RUNTIME_ROOT` for data helpers, finalization, startup, and verification.
The deployment does not accept a separate `--data` mount override.

## An asset is present but not validated

`inspect` reports presence/readability without large hashes or native reads.
`/config` and `/healthz` report operational settings and recorded readiness;
they do not validate datasets. Run:

```bash
./scripts/manage-shakemap-data.sh validate --runtime /path/to/runtime
```

Validation errors name the asset, path, problem, and corrective action. Follow
the reported cause before deciding whether the data needs replacement:

| Reported problem | Recovery |
|---|---|
| Read/traversal permission denied on managed scientific data | Use the printed data-permission helper command and rerun the failed operation. Keep the dataset in place. |
| Missing asset | Provision or manually place the pinned asset. |
| Size, format, or checksum mismatch | Review the existing asset; prepare an approved replacement if required. |
| Disk, I/O, or read-only-filesystem error | Resolve the storage or mount problem; permission repair does not fix it. |
| Write denied while importing or downloading | Correct the destination's operator write access; the data-read helper cannot grant that access. |

`provision` will not replace an invalid or unexpected existing asset. To replace
one deliberately, use the data helper's `stage` action with an approved source.
Only later finalization activates staged data when accepted work is idle. Keep
the active operator dataset until that succeeds. An unreadable file has not
been proven corrupt and must not be removed merely because validation could
not read it.

## Readiness remains false

Read the `reason` in `shake-in-docker health`. Run `make finalize` with the
intended runtime and settings after resolving its reported error. `make start`
requires matching recorded readiness and cannot finish an installation.
The latest recorded deployment attempt stopped at writable-path ownership;
see [current verification status](../README.md#current-verification-status).

## Service-state permission failure

Follow the exact bounded repair command printed by finalization, then rerun
finalization as the ordinary operator. See [permissions](permissions.md) for
the stopped/idle prerequisite and the distinction between service-write and
scientific-data-read repairs. Do not recursively chown the entire runtime.

If the repair helper itself fails, inspect its original utility error before
using any conditional `sudo` command. A generic nonzero exit does not identify
a permission problem; an I/O error or read-only mount is not fixed by elevation.

Queued or running work, malformed records, and unsafe paths have different
causes. Their errors do not prescribe a permission helper or `sudo`. Resolve
the specific refusal before retrying; do not stop accepted calculations merely
to bypass the unfinished-work check.

## Docker probe fails before checking access

Correct Docker daemon availability, image availability, or the reported mount
configuration first. A failed container operation does not justify changing
host ownership. The helper does not elevate privileges automatically.
