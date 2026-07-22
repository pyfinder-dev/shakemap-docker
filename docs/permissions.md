# Permissions

The image runs as `sysop` UID/GID `1000:1000`. Preparation checks host write
access and then verifies inside a short-lived container that UID/GID 1000 can
write:

- `incoming/`, `products/`, and `logs/`;
- `.service/events/`, `.service/work/`, `.service/archive/`;
- `.service/preparation/`.

It also attempts a write to the nested scientific-data mount and requires that
attempt to fail. If a writable path check fails, correct the ownership or ACL
on the exact runtime directory; do not recursively change an unrelated parent.

On typical Linux hosts:

```bash
sudo chown -R 1000:1000 ./runtime/shakemap
```

Review the resolved path before running that command. macOS Docker Desktop bind
mounts usually map host permissions automatically, but preparation remains the
authoritative check.
