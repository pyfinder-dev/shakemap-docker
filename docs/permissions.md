# Permissions

The container runs as UID/GID `1000:1000`.

The service needs write access to:

- `products/`;
- `logs/`;
- `.service/events/`;
- `.service/archive/`.

The external `data/` tree is mounted read-only by the supported start helper.
Use `--data DIR` to mount an existing exact tree while keeping service state in
an isolated `--runtime DIR`.

If startup reports a writable-path failure, change ownership or permissions on
the exact service-state path. Do not make scientific data writable merely to
satisfy startup: data is an external read-only input.
