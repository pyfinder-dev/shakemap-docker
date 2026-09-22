# REST API and host client

`shake-in-docker` is a thin REST client. Use `--url` before the subcommand to
select a service; the default is `http://localhost:9010`.

| REST operation | CLI operation | Information |
|---|---|---|
| `GET /healthz` | `health` | `ready`, `reason`, installed ShakeMap version |
| `GET /config` | `config` | Identity, module plan, default configuration, capacity, shared root, required-product policy, readiness |
| `GET /configurations` | `configurations` | Default and available configuration names |
| `POST /events` | `submit <event_id>` | Accept native input files and queue a caller-identified calculation |
| `GET /events` | `list` | Current and queued calculation summaries |
| `GET /events/{event_id}` | `status <event_id>` | Current, queued, and retained calculation details |
| `GET /events/{event_id}/products` | `products <event_id>` | Current product-manifest summary |
| `GET /queue` | `queue` | Queue order and capacity |

There is no `/config/profiles` compatibility route or `/v1` prefix.
Submission uses multipart fields `event_id`, `configuration`, `overwrite`, and
repeatable `files`. The CLI uploads repeatable `--file PATH` arguments. Omitted
configuration means `global`; overwrite defaults to `true`. A readable native
`event.xml` is required. The response's `internal_sequence` correlates a
submission with its later status; the public identity remains `event_id`.

Submission returns HTTP 503 while not ready. On success, poll the matching
sequence rather than interpreting product-path existence as completion. Event
detail and product summary expose operational evidence and configured shared
paths; the host client does not inspect Docker or runtime records directly.

The API does not perform full checksums or scientific validation. Use
`scripts/manage-shakemap-data.sh validate` for explicit full pinned global
asset validation.

Available configuration names do not establish dataset validity, regional
coverage, or successful execution. Public identity/provenance omit private
container paths; complete records remain in service-owned storage.
