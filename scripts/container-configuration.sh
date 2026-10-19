#!/usr/bin/env bash
# Shared fixed container identity and mount construction.

CANONICAL_IMAGE="shakemap-docker:latest"
CANONICAL_CONTAINER="shakemap-docker"
INTERNAL_PORT="9010"

acquire_finalization_lock() {
    # Re-exec, rather than supervise a child: the shell doing the deployment and
    # its cleanup own the inherited lock. Consume the one-use handoff immediately
    # so another helper launched by this process must acquire its own lock.
    if [[ "${_SHAKEMAP_FINALIZER_OWNER:-}" == "$$" && \
        "${_SHAKEMAP_FINALIZER_FD:-}" =~ ^[0-9]+$ ]]; then
        python - finalization-lock-check "${_SHAKEMAP_FINALIZER_FD}" "${_SHAKEMAP_FINALIZER_PATH}" <<'PY'
import fcntl
import os
import stat
import sys

descriptor, path = int(sys.argv[2]), sys.argv[3]
opened, named = os.fstat(descriptor), os.stat(path, follow_symlinks=False)
if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
    raise SystemExit("ERROR: finalization lock handoff is unsafe")
fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
PY
        unset _SHAKEMAP_FINALIZER_OWNER _SHAKEMAP_FINALIZER_FD _SHAKEMAP_FINALIZER_PATH
        return
    fi
    unset _SHAKEMAP_FINALIZER_OWNER _SHAKEMAP_FINALIZER_FD _SHAKEMAP_FINALIZER_PATH
    # A selected CLI context or explicit endpoint is already stable. Otherwise
    # pin the current context so a concurrent `docker context use` cannot move
    # later commands to a different daemon than the one whose lock we acquire.
    if [[ -z "${DOCKER_CONTEXT:-}" && -z "${DOCKER_HOST:-}" ]]; then
        DOCKER_CONTEXT="$(docker context show)" || return 1
        [[ -n "${DOCKER_CONTEXT}" ]] || { echo "ERROR: Docker context is unavailable." >&2; return 1; }
        export DOCKER_CONTEXT
    fi
    exec "${PROJECT_PYTHON}" - finalization-lock "$@" <<'PY'
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys

script, container, *arguments = sys.argv[2:]
try:
    daemon = subprocess.check_output(["docker", "info", "--format", "{{.ID}}"], text=True).strip()
    if not daemon:
        raise ValueError("Docker daemon identity is unavailable")
    key = hashlib.sha256((daemon + "\0" + container).encode()).hexdigest()
    path = Path("/tmp") / f"shakemap-finalization-{key}.lock"
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        descriptor = os.open(path, flags)
    else:
        # The lock stores no data. Read access permits cooperating host users
        # to lock the same inode even when their usual umask is private.
        os.fchmod(descriptor, 0o644)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        raise ValueError(f"finalization lock is not a regular file: {path}")
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("ERROR: another finalization is already running for this Docker daemon; wait for it to finish.")
    os.set_inheritable(descriptor, True)
    environment = dict(os.environ, _SHAKEMAP_FINALIZER_OWNER=str(os.getpid()),
                       _SHAKEMAP_FINALIZER_FD=str(descriptor), _SHAKEMAP_FINALIZER_PATH=str(path))
    os.execvpe("bash", ["bash", script, *arguments], environment)
except (OSError, ValueError, subprocess.CalledProcessError) as exc:
    raise SystemExit(f"ERROR: cannot acquire finalization lock: {exc}")
PY
}

inspect_finalization_mounts() {
    local inspection
    inspection="$(docker container inspect --format '{{json .}}' "${CANONICAL_CONTAINER}")" || {
        echo "ERROR: existing canonical container could not be inspected; it was left untouched." >&2
        return 1
    }
    python - finalization-mounts "${inspection}" "${RUNTIME_ABS}" "${CANONICAL_CONTAINER}" <<'PY'
import json
import re
import sys

inspection, runtime, name = sys.argv[2:]
try:
    details = json.loads(inspection)
    expected = sorted([
        ("bind", runtime, "/home/sysop/runtime", True),
        *(("bind", f"{runtime}/shakemap/data/{name}", f"/home/sysop/runtime/shakemap/data/{name}", False)
          for name in ("global", "regional", "test")),
    ])
    mounts = sorted((item["Type"], item["Source"], item["Destination"], item["RW"])
                    for item in details["Mounts"])
    if mounts != expected:
        raise ValueError(f"canonical container mounts do not belong to requested runtime {runtime}")
    if details["Name"] != f"/{name}" or not re.fullmatch(r"[0-9a-f]{64}", details["Id"]):
        raise ValueError("canonical container identity is unavailable")
except (ValueError, TypeError, KeyError) as exc:
    raise SystemExit(
        f"ERROR: {exc}; existing container and runtime were left untouched. "
        "To switch runtimes deliberately, wait for accepted work to finish, stop the previous service, "
        "explicitly remove its stopped canonical container, then rerun finalization. Preserve its runtime data."
    )
print(details["Id"])
PY
}

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
    local image_configuration_json
    if ! container_json="$(docker container inspect --format '{{json .}}' "${CANONICAL_CONTAINER}")"; then
        echo "ERROR: canonical container could not be inspected." >&2
        return 1
    fi
    if ! image_configuration_json="$(docker image inspect --format '{{json .Config}}' "${CANONICAL_IMAGE}")"; then
        echo "ERROR: canonical image configuration could not be inspected." >&2
        return 1
    fi
    python - "${mode}" "${container_json}" "${image_configuration_json}" \
        "${CANONICAL_CONTAINER}" "${CANONICAL_IMAGE}" "${IMAGE_ID}" \
        "${RUNTIME_ABS}" "${SERVICE_ABS}" "${PORT}" "${MAX_CONCURRENT}" \
        "${IMAGE_DIGEST}" <<'PY'
import json
import sys

(
    mode,
    container_json,
    image_configuration_json,
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
    image_configuration = json.loads(image_configuration_json)
except json.JSONDecodeError as exc:
    raise SystemExit(f"ERROR: malformed Docker inspection JSON: {exc}")
if not isinstance(details, dict):
    raise SystemExit("ERROR: malformed Docker container inspection data")
if not isinstance(image_configuration, dict):
    raise SystemExit("ERROR: malformed Docker image configuration data")

if details.get("Name") != f"/{container_name}":
    problems.append("container-name")
if details.get("Image") != image_id:
    problems.append("image-id")
configuration = details.get("Config")
host = details.get("HostConfig")
mounts = details.get("Mounts")
if not isinstance(configuration, dict) or configuration.get("Image") != (image_id if mode == "probe" else image_name):
    problems.append("image-reference")
if mode == "probe" and (
    configuration.get("User") != "1000:1000"
    or configuration.get("Entrypoint") != ["python"]
):
    problems.append("probe-user-or-entrypoint")
elif mode != "probe" and isinstance(configuration, dict):
    if (configuration.get("User") or "") != (image_configuration.get("User") or ""):
        problems.append("user")
    for key in ("Entrypoint", "Cmd"):
        if (configuration.get(key) or []) != (image_configuration.get(key) or []):
            problems.append(key.lower())

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
    expected_environment = environment(image_configuration.get("Env"))
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
    if mode in {"isolated", "probe"}
    else {"9010/tcp": [{"HostIp": "", "HostPort": port}]}
)
if not isinstance(host, dict) or (host.get("PortBindings") or {}) != expected_ports:
    problems.append("ports")
# Network isolation is required only while probing or verifying privately.
# Published deployments are checked by their exact port bindings, not Docker's
# descriptive network-mode label.
if mode in {"isolated", "probe"} and (
    not isinstance(host, dict) or host.get("NetworkMode") != "none"
):
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
        --filter "name=^/${CANONICAL_CONTAINER}$" --format '{{.Names}}')" || {
        echo "ERROR: canonical container presence could not be inspected." >&2
        return 1
    }
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
    if [[ "${mode}" == "isolated" || "${mode}" == "probe" ]]; then
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
    if [[ "${mode}" == "probe" ]]; then
        CONTAINER_COMMAND+=(--user 1000:1000 --entrypoint python "${IMAGE_ID}")
    else
        CONTAINER_COMMAND+=("${CANONICAL_IMAGE}")
    fi
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
        # Python consumes this heredoc on stdin; Docker must attach that stream.
        if docker exec -i "${CANONICAL_CONTAINER}" python - "${expected}" <<'PY' >/dev/null 2>&1
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
