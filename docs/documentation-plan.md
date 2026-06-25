# Documentation Architecture Plan

Date: 2026-06-24

---

## 1. Current State Assessment

### What exists today

| Document | Location | Status |
|----------|----------|--------|
| `README.md` (root) | `shakemap-docker/README.md` | **Placeholder** — 2 lines, zero useful content |
| `scripts/README.md` | `shakemap-docker/scripts/README.md` | Functional — covers workflow, script flags, REST endpoints |
| `tests/fixtures/.../README.md` | `tests/fixtures/shakemap_event_minimal/README.md` | Good — documents fixture source, format, purpose |
| `docs/` directory | Does not exist | — |

### Problems

1. A new user cloning this repository sees a 2-line README and has no idea what to do.
2. The best documentation (`scripts/README.md`) is hidden inside a subdirectory.
3. There is no documentation for the runtime directory layout, environment variables, health model, or the two-stage lifecycle.
4. Internal design knowledge (sentinel file, data bridge symlink, VS30 provisioning, profile structure) lives entirely in `agent-exchange.md` and inline code comments.
5. No troubleshooting guidance exists anywhere.

---

## 2. Documentation Principles

These principles govern all documentation decisions:

| # | Principle | Rationale |
|---|-----------|-----------|
| P1 | **README is the front door.** Any user can go from clone to running service by reading README alone. | RULE 1 — self-sufficient README |
| P2 | **README stays concise.** When a topic exceeds 1–2 paragraphs, the README links to `docs/<file>.md` immediately. | RULE 2 — link, don't inline |
| P3 | **One topic per document.** A document under `docs/` addresses exactly one concern. | RULE 3 — single responsibility |
| P4 | **No jargon without definition.** Terms like "VS30", "profile", "sentinel", "data bridge" are explained on first use. | RULE 4 — not for experts |
| P5 | **Documentation is product.** Every document is reviewed for accuracy, completeness, and consistency — same standard as code. | RULE 5 — quality parity |
| P6 | **README is the navigation hub.** Every `docs/` page is linked from a README section. Users never browse `docs/` blindly. | RULE 2 — discoverable via links |
| P7 | **Forward-compatible structure.** New documents are added to `docs/` and linked from the relevant README section. README length is bounded. | Future-proofing |

---

## 3. Proposed README Structure

### Section-by-section design

Each section below shows: the heading, its purpose, expected length, and which `docs/` file it links to (if any).

---

#### `# ShakeMap Docker Service`

**Purpose:** One-paragraph project description. What this project is, what it does, and who it's for.

**Expected length:** 3–5 sentences.

**Key content:**
- Wraps USGS ShakeMap 4 in a Docker container
- Exposes a REST API for event submission and health monitoring
- Two-stage lifecycle: build/start (infrastructure) → configure (ShakeMap readiness)
- Designed for seismic early warning pipelines

**Links to:** None. This is self-contained.

---

#### `## Prerequisites`

**Purpose:** What the user needs before starting.

**Expected length:** Short bullet list (5–8 items).

**Key content:**
- Docker (minimum version)
- Docker Buildx (if cross-platform builds needed)
- ~4 GB disk for image build
- Network access to `code.usgs.gov` (ShakeMap source) and optionally `apps.usgs.gov` (data downloads)
- Familiarity with basic Docker commands

**Links to:** None. Self-contained.

---

#### `## Quick Start`

**Purpose:** Fastest path from zero to a running, configured ShakeMap service. Four commands.

**Expected length:** 15–25 lines (commands + minimal commentary).

**Key content:**
```
1. Build:     ./scripts/build-shakemap-docker.sh
2. Start:     ./scripts/start-shakemap-docker.sh
3. Configure: docker exec shakemap /app/scripts/configure-shakemap.sh
4. Verify:    ./scripts/verify-shakemap-deployment.sh shakemap --expect ready
```
Or via Makefile: `make build && make start && make configure && make verify`

Brief explanation of what each step does (one sentence each).

**Links to:** [docs/quick-start.md](docs/quick-start.md) for the full walkthrough with expected output, common variations, and first ShakeMap execution.

---

#### `## How It Works`

**Purpose:** Explain the two-stage lifecycle at a high level so users understand why `configure` is separate from `start`.

**Expected length:** 10–15 lines.

**Key content:**
- **Stage 1 (Build/Start):** Docker image builds, container starts, infrastructure directories created, ShakeMap CLI verified, REST API starts. Service reports `not_ready`.
- **Stage 2 (Configure):** ShakeMap profile created, VS30/topo data provisioned, config files patched, readiness sentinel written. Service transitions to `healthy`.
- Why two stages: profile configuration requires a running container, data download is optional, and separation allows validation at each step.

**Links to:** [docs/architecture.md](docs/architecture.md) for detailed architecture and data flow.

---

#### `## Configuration`

**Purpose:** Environment variables, their defaults, and when to change them.

**Expected length:** Single table (12–15 rows) + 2–3 sentences of context.

**Key content:**

| Variable | Default | Purpose |
|----------|---------|---------|
| `SHAKEMAP_PROFILE` | `default` | ShakeMap profile name |
| `SHAKEMAP_PORT` | `9010` | REST API port |
| `SHAKEMAP_MODULES` | `select assemble model ...` | ShakeMap processing modules |
| `SHAKEMAP_REQUIRE_MOUNT` | `0` | Require SERVICE_ROOT to be a mount |
| `SHAKEMAP_SKIP_DATA_DOWNLOAD` | `0` | Skip USGS grid downloads |
| `SHAKEMAP_ALLOW_UNIFORM_VS30` | `0` | Allow uniform VS30 (no grid) |
| `SHAKEMAP_VS30_FILE` | (empty) | Custom VS30 grid path |
| `SHAKEMAP_TOPO_FILE` | (empty) | Custom topography grid path |

Explain: variables are set via `docker run -e` or the start script's `--env` flag.

**Links to:** [docs/configuration.md](docs/configuration.md) for detailed explanation of each variable, VS30 provisioning strategies, and profile management.

---

#### `## Runtime Directory Layout`

**Purpose:** Show what the container filesystem looks like and where outputs go.

**Expected length:** ASCII tree (10–15 lines) + brief annotations.

**Key content:**
```
/home/sysop/runtime/shakemap/    (SERVICE_ROOT)
├── events/          Event tracking + requeststatus.json
├── incoming/        Submitted input files (staged)
├── work/            ShakeMap private processing
├── products/        Published outputs (ShakeMaps)
├── archive/         Completed-run archive
├── logs/            Service logs
└── data/            Shared data (VS30, topo grids)
```

One-sentence explanation of each directory's role.

**Links to:** [docs/runtime-layout.md](docs/runtime-layout.md) for full directory tree, volume mount guidance, and file ownership requirements.

---

#### `## Health and Readiness`

**Purpose:** How to check if the service is ready and what the health statuses mean.

**Expected length:** 10–15 lines.

**Key content:**
- `GET /healthz` — returns `healthy`, `healthy_with_overrides`, or `not_ready`
- `blocking_reasons` — what's wrong; `next_action` — what to do
- Explain: service starts as `not_ready`, becomes `healthy` after Stage 2 succeeds
- Quick curl example

**Links to:** [docs/health-and-readiness.md](docs/health-and-readiness.md) for full response schema, all health statuses, override model, and Kubernetes probe configuration.

---

#### `## REST API`

**Purpose:** List all API endpoints with one-line descriptions.

**Expected length:** Short table (5–7 rows).

**Key content:**

| Method | Endpoint | Purpose |
|--------|----------|---------|
| `GET` | `/healthz` | Health and readiness |
| `GET` | `/config` | Active configuration inspection |
| `GET` | `/config/profiles` | List ShakeMap profiles |
| `POST` | `/events/submit` | Submit event for processing |
| `GET` | `/docs` | Interactive API docs (auto-generated) |

**Links to:** [docs/rest-api.md](docs/rest-api.md) for request/response schemas, examples, and error codes.

---

#### `## Running Your First ShakeMap`

**Purpose:** Walk through submitting a test event and locating the output.

**Expected length:** 10–15 lines.

**Key content:**
- Where to find the included test fixture (`tests/fixtures/shakemap_event_minimal/`)
- How to submit via curl or the REST API
- Where to find output products (`products/<event_id>/`)
- What a successful `requeststatus.json` looks like

**Links to:** [docs/execution-workflow.md](docs/execution-workflow.md) for the full execution lifecycle (submit → validate → queue → run → products).

---

#### `## Volume Mounts and Permissions`

**Purpose:** How to mount host directories and fix permission issues.

**Expected length:** 5–10 lines.

**Key content:**
- Default: `./runtime:/home/sysop/runtime`
- Container runs as `sysop` (UID 1000, GID 1000)
- Host directory must be owned by 1000:1000 on Linux
- `chown -R 1000:1000 <runtime-dir>` fix
- Entrypoint detects but cannot fix permission issues

**Links to:** [docs/permissions.md](docs/permissions.md) for platform-specific behavior (Linux, macOS Docker Desktop, Windows).

---

#### `## Scripts Reference`

**Purpose:** Quick reference for all scripts.

**Expected length:** Table (8–10 rows) with one-line descriptions.

**Key content:**

| Script | Run From | Purpose |
|--------|----------|---------|
| `build-shakemap-docker.sh` | Host | Build Docker image |
| `start-shakemap-docker.sh` | Host | Start container |
| `configure-shakemap.sh` | Container | Configure ShakeMap (Stage 2) |
| `verify-shakemap-deployment.sh` | Host | Verify running deployment |
| `inspect-shakemap-config.sh` | Container | Print active configuration |
| `verify-shakemap-build.sh` | Container | (Dev) Verify build infrastructure |
| `verify-shakemap-config.sh` | Container | (Dev) Verify configuration |
| `run-shakemap-ci-tests.sh` | Host | (Dev) Full CI test suite |

**Links to:** [scripts/README.md](scripts/README.md) for full flag documentation and usage examples (already exists).

---

#### `## Troubleshooting`

**Purpose:** Common problems and their solutions, inline.

**Expected length:** 5–8 problem/solution pairs.

**Key content:**
- "Container starts but status is `not_ready`" → Run `configure-shakemap.sh`
- "Permission denied on startup" → `chown -R 1000:1000 <dir>`
- "ShakeMap fails with CA_vs30.grd error" → VS30 not provisioned
- "Port 9010 already in use" → Change port via `--port` flag
- "`healthy_with_overrides` — what does it mean?" → Uniform VS30 override active

**Links to:** [docs/troubleshooting.md](docs/troubleshooting.md) for extended troubleshooting, log analysis, and ShakeMap-specific errors.

---

#### `## Development`

**Purpose:** How to run tests, contribute, understand the codebase.

**Expected length:** 5–10 lines.

**Key content:**
- Python test suite: `python tests/test_*.py`
- CI tests: `./scripts/run-shakemap-ci-tests.sh`
- Code structure overview (one-sentence per module)

**Links to:** [docs/developer-guide.md](docs/developer-guide.md) for module architecture, test strategy, and contribution guidelines.

---

#### `## License`

**Purpose:** License reference.

**Expected length:** 1–2 lines + link to LICENSE file.

---

### README size estimate

With this structure, the README will be approximately **150–200 lines**. This is concise enough to read in one sitting while being comprehensive enough to deploy the service.

---

## 4. Proposed `docs/` Structure

### Documents to create

Only documents that address a genuine, distinct user need.

```
docs/
├── quick-start.md
├── architecture.md
├── configuration.md
├── runtime-layout.md
├── health-and-readiness.md
├── rest-api.md
├── execution-workflow.md
├── permissions.md
├── troubleshooting.md
└── developer-guide.md
```

### Document descriptions

---

#### `docs/quick-start.md`

**Responsibility:** Extended walkthrough from clone to first ShakeMap output.

**Content:**
- Full command sequence with expected terminal output at each step
- Variations: with/without VS30, custom port, custom runtime directory
- How to verify each stage (Stage 1 → Stage 2 → submit → output)
- First event submission using the included test fixture
- Where to find and interpret outputs

**Why it exists:** README Quick Start gives the 4 commands. This document explains what each command does, what to expect, and what to do when something doesn't match.

**Expected length:** 80–120 lines.

---

#### `docs/architecture.md`

**Responsibility:** System architecture, data flow, and module responsibilities.

**Content:**
- Two-stage lifecycle diagram (Stage 1 → Stage 2 → operational)
- Service module map: `config.py`, `paths.py`, `status.py`, `submission.py`, `queue.py`, `worker.py`, `runner.py`, `main.py`
- Execution flow: submit → validate → stage → queue → claim → execute → publish
- Data bridge: how `incoming/` → `work/` → ShakeMap data directory mapping works
- ShakeMap profile structure and symlink strategy
- Queue design: filesystem-based, `requeststatus.json` as sole source of truth
- Claim locking: `fcntl.flock` on status file

**Why it exists:** Understanding the architecture is essential for debugging, extending, or operating the service. Too detailed for README.

**Expected length:** 120–180 lines.

---

#### `docs/configuration.md`

**Responsibility:** All environment variables, their interactions, and provisioning strategies.

**Content:**
- Complete environment variable reference with detailed descriptions
- VS30 provisioning: four strategies (download, mount, uniform override, bake into image)
- Profile management: what a profile is, where it lives, how to create custom profiles
- Module configuration: what each ShakeMap module does
- Configuration precedence: environment variables → defaults
- `configure-shakemap.sh` internals: what it checks, what it writes, idempotency
- Override model: what `SHAKEMAP_ALLOW_UNIFORM_VS30` does and its production implications

**Why it exists:** Configuration is the most common area where operators need detailed guidance. The README table provides a quick reference; this document provides the full context.

**Expected length:** 120–160 lines.

---

#### `docs/runtime-layout.md`

**Responsibility:** Container filesystem layout, directory purposes, and volume mounting.

**Content:**
- Full directory tree inside the container
- Purpose and contents of each directory (`events/`, `incoming/`, `work/`, `products/`, `archive/`, `logs/`, `data/`)
- Per-event directory structure: `events/<event_id>/.shakemap-service/requeststatus.json`
- ShakeMap profile directory structure: `~/shakemap_profiles/<profile>/install/`, `~/shakemap_profiles/<profile>/data/` → symlink
- Volume mount recommendations: what to mount, what not to mount
- Data persistence: what survives container restart vs. what is ephemeral

**Why it exists:** Operators need to understand where data lives to mount volumes, locate outputs, and debug issues. The README gives the top-level tree; this document gives the full picture.

**Expected length:** 80–120 lines.

---

#### `docs/health-and-readiness.md`

**Responsibility:** Complete health model documentation.

**Content:**
- Three health statuses: `healthy`, `healthy_with_overrides`, `not_ready`
- Stage 1 checks: directories, writability, ShakeMap CLI
- Stage 2 checks: profile, symlink, VS30, model.conf, sentinel
- Override model: uniform VS30 override, how it affects health status
- Sentinel file: what it is, where it lives, when it's written
- Full `/healthz` response schema with annotated example
- `blocking_reasons` and `next_action` semantics
- Kubernetes integration: `startupProbe`, `readinessProbe`, `livenessProbe` configuration

**Why it exists:** The health model is the primary operational interface. Operators and orchestrators need to understand every field.

**Expected length:** 100–140 lines.

---

#### `docs/rest-api.md`

**Responsibility:** Complete REST API reference.

**Content:**
- `GET /healthz` — full request/response with examples
- `GET /config` — full request/response with examples
- `GET /config/profiles` — full request/response with examples
- `POST /events/submit` — multipart form fields, file requirements, response codes (200, 400, 422, 503)
- Error response format
- Auto-generated docs: `/docs` (Swagger UI), `/redoc`, `/openapi.json`

**Why it exists:** API consumers need a complete reference. The README table gives an overview; this document gives the details.

**Expected length:** 100–150 lines.

---

#### `docs/execution-workflow.md`

**Responsibility:** End-to-end event processing lifecycle.

**Content:**
- Lifecycle diagram: REGISTERED → VALIDATING → QUEUED → RUNNING → SUCCESS/FAILED
- Input file requirements: `event.xml` (required), station file (required, 3 accepted formats), `rupture.json` (optional)
- Submission: what happens when `POST /events/submit` is called
- Validation: what is checked, what causes `VALIDATION_FAILED`
- Queue: how events are ordered (FIFO by `queued_at`), how the filesystem queue works
- Execution: how files move from `incoming/` → `work/` → ShakeMap, what modules run
- Products: where outputs appear (`products/<event_id>/`), atomic publication
- Retry: attempt tracking, max attempts, interrupted-attempt recovery
- `requeststatus.json` schema and example

**Why it exists:** Understanding the execution lifecycle is essential for submitting events, locating outputs, and debugging failures.

**Expected length:** 120–180 lines.

---

#### `docs/permissions.md`

**Responsibility:** File ownership, container user, and platform-specific permission behavior.

**Content:**
- Container user: `sysop` UID 1000, GID 1000
- Linux: host directory must be owned by 1000:1000
- macOS Docker Desktop: permission behavior with gRPC-FUSE/VirtioFS
- Windows Docker Desktop: synthetic permissions, `fcntl.flock` limitations
- Entrypoint permission detection: what it checks, what it cannot fix
- Suggested fix commands for each platform

**Why it exists:** Permission errors are the #1 Docker deployment issue. Platform-specific behavior is too detailed for the README.

**Expected length:** 60–80 lines.

---

#### `docs/troubleshooting.md`

**Responsibility:** Extended troubleshooting guide with log analysis and ShakeMap-specific errors.

**Content:**
- Common startup failures and fixes
- ShakeMap execution errors (VS30, GMPE, model.conf)
- Health endpoint interpretation guide
- Log locations and how to read them
- How to inspect `requeststatus.json` for debugging
- How to re-run `configure-shakemap.sh` safely
- Container restart behavior (what persists, what resets)

**Why it exists:** The README troubleshooting section covers the top 5 problems. This document covers everything else.

**Expected length:** 80–120 lines.

---

#### `docs/developer-guide.md`

**Responsibility:** Codebase structure, testing, and contribution guidance.

**Content:**
- Module map with one-paragraph description of each Python module
- Test strategy: unit tests (host-side) vs. integration tests (Docker-based)
- How to run tests: `python tests/test_*.py`, CI script
- Code conventions: atomic writes, status transitions, path functions
- How to add a new endpoint, new status, or new configuration variable
- Dependency management: `requirements.txt`, Dockerfile install

**Why it exists:** Contributors and maintainers need to understand the codebase beyond what the user-facing docs cover.

**Expected length:** 80–120 lines.

---

### Documents NOT created (and why)

| Rejected Document | Reason |
|-------------------|--------|
| `docs/installation.md` | Installation is covered in README Quick Start + `docs/quick-start.md`. A separate install doc would duplicate content. |
| `docs/profiles.md` | Profile management is part of `docs/configuration.md`. Not enough standalone content for a separate document. |
| `docs/deployment.md` | Deployment is covered by README (Quick Start, Volume Mounts, Scripts Reference) + `docs/quick-start.md`. A separate deployment doc would fragment the narrative. |
| `docs/cli.md` | The service has no standalone CLI. Scripts are documented in `scripts/README.md`. |
| `docs/overview.md` | The README "How It Works" section serves this purpose. A separate overview would duplicate the README. |
| `docs/runtime-data.md` | Merged into `docs/runtime-layout.md`. Data and layout are the same concern. |

---

## 5. Documentation Ownership Matrix

For every topic, where should it live?

| Topic | README.md | docs/ file | Both? | Notes |
|-------|-----------|------------|-------|-------|
| Project description | ✅ Primary | — | — | README only |
| Prerequisites | ✅ Primary | — | — | README only |
| Quick start (4 commands) | ✅ Primary | `quick-start.md` | README = commands, docs = extended walkthrough | README links to docs |
| Two-stage lifecycle | ✅ Summary | `architecture.md` | README = overview, docs = detail | README links to docs |
| Environment variables | ✅ Table | `configuration.md` | README = quick reference table, docs = full explanations | README links to docs |
| VS30 provisioning | — | `configuration.md` | — | Too detailed for README |
| Runtime directory layout | ✅ Tree | `runtime-layout.md` | README = top-level tree, docs = full tree + guidance | README links to docs |
| Health statuses | ✅ Summary | `health-and-readiness.md` | README = 3 statuses + curl, docs = full schema | README links to docs |
| REST API endpoints | ✅ Table | `rest-api.md` | README = endpoint table, docs = full schemas | README links to docs |
| First ShakeMap execution | ✅ Summary | `execution-workflow.md` | README = how to submit, docs = full lifecycle | README links to docs |
| Event lifecycle (statuses) | — | `execution-workflow.md` | — | Too detailed for README |
| Volume mounts | ✅ Summary | `permissions.md` | README = basic mount + chown, docs = platform-specific | README links to docs |
| Permissions (platform) | — | `permissions.md` | — | Too detailed for README |
| Script reference | ✅ Table | `scripts/README.md` | README = summary table, scripts/README = full flags | README links to scripts/README |
| Troubleshooting | ✅ Top 5 | `troubleshooting.md` | README = common problems, docs = full guide | README links to docs |
| Development / testing | ✅ Summary | `developer-guide.md` | README = how to run tests, docs = full guide | README links to docs |
| Architecture / internals | — | `architecture.md` | — | Not for README |
| Queue design | — | `architecture.md` | — | Internal detail |
| Claim locking | — | `architecture.md` | — | Internal detail |
| Override model | — | `health-and-readiness.md` | — | Operational detail |
| Sentinel file | — | `health-and-readiness.md` | — | Operational detail |
| Kubernetes probes | — | `health-and-readiness.md` | — | Deployment detail |
| Test fixture format | — | `tests/fixtures/.../README.md` | — | Stays where it is |
| License | ✅ Primary | — | — | README only |

---

## 6. Cross-Linking Strategy

### Principle

> A user should never need to browse the `docs/` directory. Every document is reachable from a README section.

### Implementation

1. **Every README section that summarizes a topic links to the corresponding `docs/` file.** The link appears at the end of the section, formatted consistently:

   ```markdown
   → See [Configuration Guide](docs/configuration.md) for detailed variable descriptions and VS30 provisioning.
   ```

2. **`docs/` files link back to README** when they reference a concept covered elsewhere:

   ```markdown
   See the [Quick Start](../README.md#quick-start) section in the README.
   ```

3. **`docs/` files cross-link to each other** when topics are related:

   ```markdown
   For health status definitions, see [Health and Readiness](health-and-readiness.md).
   ```

4. **No orphan documents.** Every `docs/` file must be linked from at least one README section.

5. **Link text is descriptive.** Use `[Configuration Guide](docs/configuration.md)`, not `[click here](docs/configuration.md)` or `[docs/configuration.md](docs/configuration.md)`.

### Cross-link map

```
README.md
├── Quick Start ──────────────→ docs/quick-start.md
├── How It Works ─────────────→ docs/architecture.md
├── Configuration ────────────→ docs/configuration.md
├── Runtime Directory Layout ─→ docs/runtime-layout.md
├── Health and Readiness ─────→ docs/health-and-readiness.md
├── REST API ─────────────────→ docs/rest-api.md
├── Running Your First ShakeMap → docs/execution-workflow.md
├── Volume Mounts / Permissions → docs/permissions.md
├── Scripts Reference ────────→ scripts/README.md
├── Troubleshooting ──────────→ docs/troubleshooting.md
└── Development ──────────────→ docs/developer-guide.md

docs/quick-start.md
├── → docs/configuration.md (env vars)
├── → docs/runtime-layout.md (where outputs go)
└── → docs/troubleshooting.md (if something goes wrong)

docs/architecture.md
├── → docs/execution-workflow.md (event lifecycle)
├── → docs/runtime-layout.md (directory structure)
└── → docs/health-and-readiness.md (health model)

docs/configuration.md
├── → docs/permissions.md (volume mount setup)
├── → docs/health-and-readiness.md (override model)
└── → docs/quick-start.md (getting started)

docs/execution-workflow.md
├── → docs/rest-api.md (submission endpoint)
├── → docs/runtime-layout.md (where files go)
└── → docs/troubleshooting.md (common failures)

docs/health-and-readiness.md
├── → docs/configuration.md (override variables)
├── → docs/rest-api.md (endpoint details)
└── → docs/troubleshooting.md (interpreting status)
```

---

## 7. Future-Proofing

### How to add new documentation without growing README

1. **New features that extend an existing topic** — add content to the relevant `docs/` file. No README change needed.

   Example: Adding a new environment variable → add to the table in `docs/configuration.md`. The README table stays at the core variables only.

2. **New features that create a new topic** — create a new `docs/` file and add a single link from the most relevant README section.

   Example: Adding Kubernetes deployment support → create `docs/kubernetes.md` and add a line to the README "Health and Readiness" section: *"→ See [Kubernetes Deployment](docs/kubernetes.md) for probe configuration and Helm chart."*

3. **README sections are permanent anchors.** The README section headings should rarely change. New topics attach to existing sections via links.

4. **README table of contents (implicit).** The section headings serve as a natural table of contents. If the README exceeds ~250 lines, add an explicit `## Contents` section with anchor links.

5. **`docs/` naming convention.** Use kebab-case, descriptive names. No phase numbers, no abbreviations.

   - Good: `docs/health-and-readiness.md`, `docs/kubernetes.md`
   - Bad: `docs/h-and-r.md`, `docs/phase08-k8s.md`

6. **`scripts/README.md` remains independent.** It is the script-specific reference. The root README links to it but does not absorb it.

### Version documentation

When the service API or behavior changes significantly:

- Update the relevant `docs/` file.
- If a breaking change affects the Quick Start or Configuration, update the README section and the linked doc.
- Consider adding a `## Changelog` section to the README or a `docs/changelog.md` for significant releases.

---

## 8. Implementation Order

When writing the documentation (future task), follow this sequence:

| Step | Document | Rationale |
|------|----------|-----------|
| 1 | `README.md` | Foundation — establishes structure and links |
| 2 | `docs/quick-start.md` | Most critical for new users |
| 3 | `docs/configuration.md` | Most common operational need |
| 4 | `docs/runtime-layout.md` | Required to understand outputs |
| 5 | `docs/health-and-readiness.md` | Required for operational monitoring |
| 6 | `docs/rest-api.md` | Required for API consumers |
| 7 | `docs/execution-workflow.md` | Required for event processing understanding |
| 8 | `docs/permissions.md` | Required for Linux deployments |
| 9 | `docs/troubleshooting.md` | Required for self-service problem resolution |
| 10 | `docs/architecture.md` | Important but not blocking for operators |
| 11 | `docs/developer-guide.md` | Important for contributors, not blocking for users |

Steps 1–6 should be completed together — they form the minimum viable documentation set. Steps 7–11 can follow incrementally.

---

## 9. Content Sources

The following existing materials contain authoritative information that should be extracted into the new documentation:

| Source | Contains |
|--------|----------|
| `scripts/README.md` | Script flags, workflow commands, endpoint list |
| `entrypoint.sh` | Stage 1 steps, directory creation, environment defaults |
| `scripts/configure-shakemap.sh` | Stage 2 steps, readiness probes, sentinel format |
| `shakemap_service/config.py` | All environment variables and defaults |
| `shakemap_service/paths.py` | Directory layout, path functions |
| `shakemap_service/main.py` | REST API endpoints, health response schema |
| `shakemap_service/status.py` | Event lifecycle statuses, `requeststatus.json` schema |
| `shakemap_service/runner.py` | Execution bridge, product publication |
| `shakemap_service/submission.py` | Input file validation, accepted filenames |
| `.agent/agent-exchange.md` | Architecture decisions, root cause analyses, VS30 findings |
| `Makefile` | Convenience targets |
| `Dockerfile` | Build process, system packages, user creation |
| `tests/fixtures/shakemap_event_minimal/README.md` | Input file format specifications |
