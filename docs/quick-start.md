# Quick start

From the repository root:

```bash
source ../.venv/bin/activate
./scripts/build-shakemap-docker.sh
./scripts/configure-shakemap.sh
./scripts/start-shakemap-docker.sh
curl -fsS http://localhost:9010/config | python -m json.tool
curl -fsS http://localhost:9010/healthz | python -m json.tool
```

Preparation precedes creation of the stable container. It reuses valid global
grids, downloads only missing or explicitly invalid files, provisions external
Slab2 support, generates the mounted global base snapshot, and executes both
fixed offline native checks.

For manual data placement:

```bash
./scripts/configure-shakemap.sh \
  --vs30-source /path/global_vs30.grd \
  --topo-source /path/topo_30sec.grd \
  --slab-source /path/slab2.zip \
  --no-download
```

If `shakemap-docker` already exists, the start helper leaves it untouched. The
operator must explicitly stop/remove or resume it. Reusing `./runtime` retains
the preparation snapshot and evidence across container recreation.
