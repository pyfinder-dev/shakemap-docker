#!/usr/bin/env bash
# Shared fixed container identity and mount construction.

CANONICAL_IMAGE="shakemap-docker:latest"
CANONICAL_CONTAINER="shakemap-docker"
INTERNAL_PORT="9010"

require_project_environment() {
    if [[ -z "${VIRTUAL_ENV:-}" ]]; then
        echo "ERROR: activate the project Python environment before continuing." >&2
        return 1
    fi
    PROJECT_PYTHON="${VIRTUAL_ENV}/bin/python"
    PROJECT_CLI="${VIRTUAL_ENV}/bin/shake-in-docker"
    if [[ ! -x "${PROJECT_PYTHON}" || "$(command -v python 2>/dev/null || true)" != "${PROJECT_PYTHON}" ]]; then
        echo "ERROR: python must resolve from the active project environment." >&2
        return 1
    fi
    if [[ ! -x "${PROJECT_CLI}" || "$(command -v shake-in-docker 2>/dev/null || true)" != "${PROJECT_CLI}" ]]; then
        echo "ERROR: shake-in-docker is not installed in the active project environment." >&2
        return 1
    fi
}

validate_deployment_settings() {
    if [[ ! "${PORT}" =~ ^[0-9]+$ || "${PORT}" -lt 1 || "${PORT}" -gt 65535 ]]; then
        echo "ERROR: PORT must be an integer from 1 through 65535." >&2
        return 1
    fi
    if [[ ! "${MAX_CONCURRENT}" =~ ^[0-9]+$ || "${MAX_CONCURRENT}" -lt 1 ]]; then
        echo "ERROR: MAX_CONCURRENT must be a positive integer." >&2
        return 1
    fi
}

resolve_runtime_root() {
    if [[ ! -d "${RUNTIME_ROOT}" ]]; then
        echo "ERROR: runtime root does not exist: ${RUNTIME_ROOT}; run make finalize." >&2
        return 1
    fi
    RUNTIME_ABS="$(cd "${RUNTIME_ROOT}" && pwd -P)"
    SERVICE_ABS="${RUNTIME_ABS}/shakemap"
    for path in data/global data/regional data/test data/inputs products logs .service; do
        if [[ ! -d "${SERVICE_ABS}/${path}" ]]; then
            echo "ERROR: finalized runtime path is missing: ${SERVICE_ABS}/${path}; run make finalize." >&2
            return 1
        fi
    done
}

load_image_identity() {
    IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${CANONICAL_IMAGE}")"
    IMAGE_RELEASE="$(docker image inspect --format '{{index .Config.Labels "org.usgs.shakemap.release"}}' "${CANONICAL_IMAGE}")"
    IMAGE_VERSION="$(docker image inspect --format '{{index .Config.Labels "org.usgs.shakemap.version"}}' "${CANONICAL_IMAGE}")"
    IMAGE_COMMIT="$(docker image inspect --format '{{index .Config.Labels "org.usgs.shakemap.commit"}}' "${CANONICAL_IMAGE}")"
    IMAGE_DIGEST="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "${CANONICAL_IMAGE}" | head -n 1)"
    if [[ ! "${IMAGE_ID}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
        echo "ERROR: canonical image identity is unavailable." >&2
        return 1
    fi
}

verify_canonical_container_configuration() {
    local mode="$1"
    local container_json
    local image_environment_json
    if ! container_json="$(docker container inspect --format '{{json .}}' "${CANONICAL_CONTAINER}")"; then
        echo "ERROR: canonical container could not be inspected." >&2
        return 1
    fi
    if ! image_environment_json="$(docker image inspect --format '{{json .Config.Env}}' "${CANONICAL_IMAGE}")"; then
        echo "ERROR: canonical image environment could not be inspected." >&2
        return 1
    fi
    python - "${mode}" "${container_json}" "${image_environment_json}" \
        "${CANONICAL_CONTAINER}" "${CANONICAL_IMAGE}" "${IMAGE_ID}" \
        "${RUNTIME_ABS}" "${SERVICE_ABS}" "${PORT}" "${MAX_CONCURRENT}" \
        "${IMAGE_DIGEST}" <<'PY'
import json
import sys

(
    mode,
    container_json,
    image_environment_json,
    container_name,
    image_name,
    image_id,
    runtime_root,
    service_root,
    port,
    maximum,
    image_digest,
) = sys.argv[1:]
problems = []
try:
    details = json.loads(container_json)
    image_environment = json.loads(image_environment_json)
except json.JSONDecodeError as exc:
    raise SystemExit(f"ERROR: malformed Docker inspection JSON: {exc}")
if not isinstance(details, dict):
    raise SystemExit("ERROR: malformed Docker container inspection data")

if details.get("Name") != f"/{container_name}":
    problems.append("container-name")
if details.get("Image") != image_id:
    problems.append("image-id")
configuration = details.get("Config")
host = details.get("HostConfig")
mounts = details.get("Mounts")
if not isinstance(configuration, dict) or configuration.get("Image") != image_name:
    problems.append("image-reference")

expected_mounts = sorted(
    [
        ("bind", runtime_root, "/home/sysop/runtime", True),
        ("bind", f"{service_root}/data/global", "/home/sysop/runtime/shakemap/data/global", False),
        ("bind", f"{service_root}/data/regional", "/home/sysop/runtime/shakemap/data/regional", False),
        ("bind", f"{service_root}/data/test", "/home/sysop/runtime/shakemap/data/test", False),
    ]
)
try:
    actual_mounts = sorted(
        (item["Type"], item["Source"], item["Destination"], item["RW"])
        for item in mounts
    )
except (KeyError, TypeError):
    actual_mounts = None
if actual_mounts != expected_mounts:
    problems.append("mounts")

def environment(values):
    if values is None:
        values = []
    if not isinstance(values, list):
        raise ValueError
    result = {}
    for entry in values:
        if not isinstance(entry, str) or "=" not in entry:
            raise ValueError
        key, value = entry.split("=", 1)
        if not key or key in result:
            raise ValueError
        result[key] = value
    return result

try:
    expected_environment = environment(image_environment)
    actual_environment = environment(configuration.get("Env"))
except (AttributeError, ValueError):
    expected_environment = None
    actual_environment = None
expected_values = {
    "SHAKEMAP_IMAGE_ID": image_id,
    "SHAKEMAP_SHARED_RUNTIME_ROOT": runtime_root,
    "SHAKEMAP_MAX_CONCURRENT": maximum,
}

expected_environment = expected_environment or {}
for key in (
    "SHAKEMAP_IMAGE_ID",
    "SHAKEMAP_SHARED_RUNTIME_ROOT",
    "SHAKEMAP_MAX_CONCURRENT",
    "SHAKEMAP_IMAGE_DIGEST",
):
    expected_environment.pop(key, None)
expected_environment.update(expected_values)
if image_digest:
    expected_environment["SHAKEMAP_IMAGE_DIGEST"] = image_digest
if actual_environment != expected_environment:
    problems.append("environment")

expected_ports = (
    {}
    if mode == "isolated"
    else {"9010/tcp": [{"HostIp": "", "HostPort": port}]}
)
network_mode = "none" if mode == "isolated" else "default"
if not isinstance(host, dict) or (host.get("PortBindings") or {}) != expected_ports:
    problems.append("ports")
if not isinstance(host, dict) or host.get("NetworkMode") != network_mode:
    problems.append("network-mode")

if problems:
    raise SystemExit(
        "ERROR: canonical container configuration differs: "
        + ", ".join(problems)
    )
PY
}

canonical_container_presence() {
    local containers
    containers="$(docker container ls --all \
        --filter "name=^/${CANONICAL_CONTAINER}$" --format '{{.Names}}')"
    if [[ -z "${containers}" ]]; then
        echo absent
    elif [[ "${containers}" == "${CANONICAL_CONTAINER}" ]]; then
        echo present
    else
        echo "ERROR: Docker returned an unexpected canonical-container listing." >&2
        return 1
    fi
}

container_command() {
    local action="$1"
    local mode="$2"
    CONTAINER_COMMAND=(docker "${action}")
    if [[ "${action}" == "run" ]]; then
        CONTAINER_COMMAND+=(-d)
    elif [[ "${action}" != "create" ]]; then
        echo "ERROR: unknown container action: ${action}" >&2
        return 1
    fi
    CONTAINER_COMMAND+=(--name "${CANONICAL_CONTAINER}")
    if [[ "${mode}" == "isolated" ]]; then
        CONTAINER_COMMAND+=(--network none)
    elif [[ "${mode}" == "published" ]]; then
        CONTAINER_COMMAND+=(-p "${PORT}:${INTERNAL_PORT}")
    else
        echo "ERROR: unknown container mode: ${mode}" >&2
        return 1
    fi
    CONTAINER_COMMAND+=(
        -v "${RUNTIME_ABS}:/home/sysop/runtime"
        -v "${SERVICE_ABS}/data/global:/home/sysop/runtime/shakemap/data/global:ro"
        -v "${SERVICE_ABS}/data/regional:/home/sysop/runtime/shakemap/data/regional:ro"
        -v "${SERVICE_ABS}/data/test:/home/sysop/runtime/shakemap/data/test:ro"
        -e "SHAKEMAP_IMAGE_ID=${IMAGE_ID}"
        -e "SHAKEMAP_SHARED_RUNTIME_ROOT=${RUNTIME_ABS}"
        -e "SHAKEMAP_MAX_CONCURRENT=${MAX_CONCURRENT}"
    )
    [[ -n "${IMAGE_DIGEST}" ]] && CONTAINER_COMMAND+=(-e "SHAKEMAP_IMAGE_DIGEST=${IMAGE_DIGEST}")
    CONTAINER_COMMAND+=("${CANONICAL_IMAGE}")
}

container_run_command() {
    container_command run "$1"
}

container_create_command() {
    container_command create "$1"
}

wait_for_container_health() {
    local expected="$1"
    local attempt
    for attempt in $(seq 1 60); do
        if docker exec "${CANONICAL_CONTAINER}" python - "${expected}" <<'PY' >/dev/null 2>&1
import json
import sys
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:9010/healthz", timeout=2) as response:
    payload = json.load(response)
raise SystemExit(0 if payload.get("ready") is (sys.argv[1] == "true") else 1)
PY
        then
            return 0
        fi
        sleep 1
    done
    echo "ERROR: canonical service did not report ready=${expected}." >&2
    return 1
}
