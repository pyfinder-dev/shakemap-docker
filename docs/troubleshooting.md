# Troubleshooting

## Service reports `not_ready`

Inspect both public responses:

```bash
curl -fsS http://localhost:9010/config | python -m json.tool
curl -fsS http://localhost:9010/healthz | python -m json.tool
```

Then rerun the host preparation command. The last durable attempt is under
`runtime/shakemap/.service/preparation/attempts/`; detailed native logs are
under `runtime/shakemap/.service/preparation/logs/`.

## Existing global file is rejected

The report states whether size, HDF5 signature, digest, raster format, bounds,
or coverage failed. Preparation downloads/imports a replacement to a temporary
sibling first and preserves the rejected file with an `invalid-<timestamp>`
suffix. It never deletes a valid file.

## Download unavailable

Place official files manually and use `--vs30-source`, `--topo-source`, and
`--slab-source` with `--no-download`. The same digest and native validations
apply.

## California package migration

The old generated package contained duplicate STREC and Natural Earth data.
Migration occurs only after every old manifest entry matches its recorded
digest and the corrected package validates separately. The old folder is
preserved as `v4.4.9.legacy-<timestamp>-<suffix>`. Unknown operator folders are
not changed.

## Native verification fails

Do not weaken the check or substitute the California package for the global
run. Inspect `native/california/` or `native/global/` stdout, stderr, evidence,
configuration inventory, and output inventory. A failed attempt does not become
the current preparation manifest.

## Container already exists

The start helper preserves it. Resume it explicitly, or explicitly stop/remove
it before creating a replacement. Reusing the prepared runtime is supported.
