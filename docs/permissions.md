# Permissions

The container runs as `sysop`, UID/GID `1000:1000`. It needs write access below
`<RUNTIME_ROOT>/shakemap/` to `products/`, `logs/`, `.service/`, and
`data/inputs/`. This includes `.service/events/`, `.service/archive/`,
`.service/queue/`, and service coordination files.

Finalization checks actual container operations with the deployment bind
mounts. If they succeed, it does not change host ownership or modes. Host
numeric ownership can differ from the ownership presented by Docker Desktop.
If access fails, finalization attempts the bounded writable repair with the
invoking user's existing privileges and checks access again. Only confirmed
permission-denied errors authorize this repair. Docker errors, a full disk,
I/O errors, read-only filesystems, unsafe layouts, and probe cleanup failures
stop the workflow without ownership repair.

## Manual service-writable repair

If the automatic repair lacks authority, finalization prints a command with
the exact helper and runtime paths. Before running it, let accepted work finish,
stop the canonical service, and ensure no other process is changing the runtime.
Do not stop running calculations merely to force permission repair.

For example, from the repository root:

```bash
sudo ./scripts/repair-shakemap-writable-paths.sh --runtime-root /absolute/path/to/runtime
```

This helper uses host utilities; it does not need Python or Docker and never
starts a service. It repairs only the four writable trees for `1000:1000`,
preserving contents and existing group/other permissions. It rejects unsafe
paths before repair. Fix a reported unsafe layout before retrying; `sudo` does
not make a symbolic link or an unexpected file type acceptable.

A failed shell utility does not by itself establish insufficient privilege.
The helper preserves the utility's error and reports the affected operation
and path. Use its conditional privileged command only after confirming an
authorization failure; storage, mount, and tool failures need their own remedy.
The finalizer preserves a failed helper's diagnostics and exit status without
adding another privilege recommendation.

Then rerun `make finalize` as the ordinary operator with the project environment
active and the same runtime, port, and concurrency settings. Do not run the
entire finalizer with `sudo`. If access still fails, inspect the reported mount
or filesystem restriction; ownership changes cannot repair every mount policy.

On native Linux, transferring a private `0700` directory or `0600` state file
to UID 1000 can exclude a host operator with a different UID. Host finalization
also needs access to those records. Before privileged repair, ensure the
operator identity or an explicitly configured filesystem access policy supports
both host administration and container execution. The helper does not configure
ACLs or broaden group/other permissions. This combination still requires real
deployment verification; permission repair alone is not a portability claim.

## Scientific-data read access

`data/global/`, `data/regional/`, and `data/test/` are operator-owned and mounted
read-only. Writable repair never changes them. Global-data validation and
provisioning distinguish access denial from missing or invalid content and
report the existing data helper with the affected target and selected runtime.
Follow that diagnostic; users do not need to select a repair policy themselves.
Run the command on the host and rerun the failed data action afterward. If the
operator lacks permission to perform the indicated repair, use the explicitly
shown manual `sudo` form.

For data copied with restrictive read permissions, the underlying commands are:

```bash
make fix-permissions RUNTIME_ROOT=./runtime
# Or select exactly one existing subtree or file:
make fix-permissions RUNTIME_ROOT=./runtime PERMISSION_TARGET=global/vs30
```

It adds read/traversal permissions while preserving ownership, write bits,
special bits, and file contents. Missing default folders are skipped. If this
operation requires elevated host authority, invoke that same helper manually:

```bash
sudo ./scripts/fix-shakemap-permissions.sh \
  --runtime-root /absolute/path/to/runtime --target global/vs30
```

Neither permission helper establishes checksum validity, scientific suitability,
or deployment readiness. Never apply recursive ownership or broad writable
permissions to the whole runtime or scientific-data tree.

Read repair does not grant write access needed to install a missing dataset,
fix an unreadable source outside the managed data tree, or repair inaccessible
ancestors outside its allowed scope. Those failures require the operator to
correct the specific source, parent directory, or mount access reported by the
command. Do not apply the service-ownership helper to scientific data.
