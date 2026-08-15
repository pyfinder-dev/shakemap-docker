# Troubleshooting

## An asset is reported absent

Confirm that the service mounted the intended exact tree:

```bash
./scripts/manage-shakemap-data.sh inspect --runtime /path/to/runtime
```

An empty disposable runtime proves only that its own data tree is empty. If
using data stored elsewhere, start with `--data /exact/shakemap/data`.

## An asset is present but not validated

This is expected from `/config`, `/healthz`, and `inspect`; those paths avoid
large hashes and native reads. Run:

```bash
./scripts/manage-shakemap-data.sh validate --runtime /path/to/runtime
```

Validation errors name the asset, path, problem, and corrective action.
`provision` will not replace an invalid or unexpected existing asset. Review it,
then move or remove it explicitly before installing the pinned asset, or correct
the existing file manually and rerun validation.

## Overall readiness remains false

This is the correct current state. Dataset presence or validation cannot enable
managed calculations because effective configuration resolution is not
implemented.

## Service-state permission failure

Ensure UID/GID `1000:1000` can write `products/`, `logs/`,
`.service/events/`, and `.service/archive/`. Keep `data/` read-only.
