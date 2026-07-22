# ShakeMap Docker scripts

Run host commands from the repository root. Host-side Python commands must use
the existing project environment:

```bash
cd /Users/savas/my-codes/eew/pyfinder-dev/shakemap-docker
source /Users/savas/my-codes/eew/pyfinder-dev/.venv/bin/activate
docker info
```

Building requires Docker, Git, the project environment, free image space, and
network access to the official USGS GitLab service plus Python package indexes.
Configuration can require additional USGS data downloads.

## Build and release resolution

The default build resolves the latest final stable USGS ShakeMap release and
its full commit once, then passes that immutable pair into Docker:

```bash
./scripts/build-shakemap-docker.sh --tag shakemap-service:identity-check
```

A pinned rebuild requires both values. The helper verifies that the full commit
is the official target of the final stable tag; branches, prereleases, short
commits, mismatched pairs, and one-sided overrides fail closed:

```bash
./scripts/build-shakemap-docker.sh \
  --tag shakemap-service:identity-check \
  --release-tag vX.Y.Z \
  --release-commit 0123456789abcdef0123456789abcdef01234567
```

Other build flags are `--platform PLAT`, `--no-cache`, and `--help`.

## Host-side verification

These checks do not prove that the image or running service works:

```bash
source /Users/savas/my-codes/eew/pyfinder-dev/.venv/bin/activate
python -m py_compile \
  shakemap_service/release.py \
  shakemap_service/build_identity.py \
  tests/test_release_resolution.py \
  tests/test_build_identity.py
bash -n scripts/build-shakemap-docker.sh
bash -n scripts/start-shakemap-docker.sh
bash -n scripts/verify-shakemap-image.sh
python tests/test_release_resolution.py
python tests/test_build_identity.py
git diff --check
```

## Image labels and the immutable manifest

OCI labels are a registry-visible summary. Inspect them on the host:

```bash
docker image inspect shakemap-service:identity-check \
  --format '{{json .Config.Labels}}' | python -m json.tool
```

The read-only `/opt/shakemap-build/identity.json` manifest is the validated
authoritative in-container record. It includes the official upstream URL,
stable tag and full commit; installed `shakemap` and `shakemap-modules`
versions; Python version; dependency-inventory path and digest; service commit
and dirty state when available; and build timestamp. Its code-fixed path cannot
be redirected by an environment variable.

The Docker image ID and optional repository digest are not build-manifest
facts. They describe the deployed image and are collected by the supported
startup helper. A locally built/loaded image commonly has an image ID but no
repository digest; that absence is reported as unavailable rather than guessed.

## Container-internal and running-service verification

Use durable verification names and a distinct port/runtime directory:

```bash
./scripts/start-shakemap-docker.sh \
  --name shakemap-identity-check \
  --runtime /private/tmp/shakemap-identity-check-runtime \
  --port 19010 \
  --image shakemap-service:identity-check \
  --env IDENTITY_CHECK_MARKER=present
```

Confirm that ordinary environment variables reached the container, the
manifest remains fixed even if a process supplies the old redirect variable,
and all image-internal checks pass:

```bash
docker exec shakemap-identity-check env | grep '^IDENTITY_CHECK_MARKER=present$'
docker exec -e SHAKEMAP_BUILD_IDENTITY_FILE=/tmp/redirect.json \
  shakemap-identity-check python -c \
  'from shakemap_service.build_identity import load_build_identity; assert load_build_identity()["immutable_image"]["available"]'
docker exec shakemap-identity-check \
  python -m json.tool /opt/shakemap-build/identity.json
docker exec shakemap-identity-check /app/scripts/verify-shakemap-image.sh
```

Then check the live endpoints and prove they expose the same shared identity:

```bash
curl -fsS http://localhost:19010/config | python -m json.tool
curl -fsS http://localhost:19010/healthz | python -m json.tool
python -c 'import json, urllib.request as u; c=json.load(u.urlopen("http://localhost:19010/config")); h=json.load(u.urlopen("http://localhost:19010/healthz")); assert c["identity"] == h["identity"]; print(json.dumps(c["identity"], indent=2))'
```

`not_ready` is an expected service state before scientific configuration. A
successful build, a valid manifest, passing imports, or the existence of some
products does not prove calculation readiness. Readiness requires valid data;
uniform VS30 is only an explicit development/emergency override. Successful
event processing additionally requires validated core products, provenance, a
product manifest, and logs. Calculation-level deployment proof is deferred
until release-matched test data and the later calculation work are available.

## Startup helper environment rules

`--env KEY=VALUE` remains supported for ordinary variables. These keys are
reserved and rejected before Docker is invoked:

- `SHAKEMAP_IMAGE_ID`
- `SHAKEMAP_IMAGE_DIGEST`
- `SHAKEMAP_BUILD_IDENTITY_FILE`

For example, this must fail:

```bash
./scripts/start-shakemap-docker.sh --env SHAKEMAP_IMAGE_ID=untrusted
```

The restriction protects the supported helper path, not arbitrary direct
`docker run` commands. The service validates supported Docker formats before it
reports supplied deployment values as trusted facts.

## Other scripts and current limitations

- `configure-shakemap.sh` configures a running profile and data.
- `verify-shakemap-deployment.sh` checks a running container; it does not build one.
- `inspect-shakemap-config.sh` and `inspect-shakemap-events.sh` inspect existing state.
- `verify-shakemap-build.sh`, `verify-shakemap-config.sh`, and
  `run-shakemap-ci-tests.sh` are broader legacy/later-capability checks. Their
  presence is not a claim that later event, queue, retry, regional-data, or
  scientific-readiness milestones are complete.

## Cleanup of the verification deployment

The following removes only the container and runtime directory created by the
commands above:

```bash
docker rm -f shakemap-identity-check
rm -rf -- /private/tmp/shakemap-identity-check-runtime
```
