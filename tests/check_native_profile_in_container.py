"""Exercise private profile materialization in the supported container."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from shakemap_service import (
    native_context,
    native_profile,
    paths,
    recalculation,
    runner,
    status,
)
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


CHECK_ROOT = Path("/tmp/m5c-check")
RUNTIME_ROOT = CHECK_ROOT / "runtime"
FIXTURE_FILE = CHECK_ROOT / "fixture" / "event.xml"
MOUNTED_DATA_ROOT = Path("/home/sysop/runtime/shakemap/data")
REGIONAL_SEED = Path("/opt/shakemap-seeds/regional/romania")
GLOBAL_EVENT_ID = "m5c-global-check"
REGIONAL_EVENT_ID = "m5c-regional-check"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configure_runtime() -> None:
    configured = Settings(
        runtime_root=str(RUNTIME_ROOT),
        shared_runtime_root=str(RUNTIME_ROOT),
    )
    paths.settings = configured
    status.settings = configured


def _link_global_data() -> dict[Path, os.stat_result]:
    relative_paths = (
        Path("global/vs30/global_vs30.grd"),
        Path("global/topo/topo_30sec.grd"),
    )
    source_state: dict[Path, os.stat_result] = {}
    for relative in relative_paths:
        source = MOUNTED_DATA_ROOT / relative
        if not source.is_file() or not os.access(source, os.R_OK):
            raise RuntimeError(f"mounted global source is unavailable: {source}")
        source_state[source] = source.stat()
        destination = paths.shakemap_data_dir() / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source)
    return source_state


def _prepare_context(
    event_id: str,
    configuration: str,
) -> native_context.NativeCalculationContext:
    with FIXTURE_FILE.open("rb") as fixture:
        accepted = accept_request(
            event_id,
            [Upload("event.xml", fixture)],
            configuration=configuration,
        )
    status.transition_to_running(accepted.internal_sequence)
    prepared = recalculation.prepare_calculation(accepted.internal_sequence)
    return native_context.prepare_native_context(
        prepared.record,
        dict(os.environ),
    )


def _resolved_native_paths(
    context: native_context.NativeCalculationContext,
) -> tuple[Path, Path]:
    script = (
        "import json; "
        "from shakemap_modules.utils.config import get_config_paths; "
        "print(json.dumps([str(value) for value in get_config_paths()]))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=context.environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"get_config_paths failed: {completed.stdout}")
    values = json.loads(completed.stdout.strip().splitlines()[-1])
    if not isinstance(values, list) or len(values) != 2:
        raise RuntimeError(f"unexpected get_config_paths result: {values!r}")
    return Path(values[0]), Path(values[1])


def _global_check() -> dict[str, object]:
    context = _prepare_context(GLOBAL_EVENT_ID, "global")
    profile = native_profile.materialize_native_profile(context)
    resolved_install, resolved_data = _resolved_native_paths(context)
    if resolved_install != context.install_directory:
        raise RuntimeError("private install path did not resolve exactly")
    if resolved_data != context.data_directory:
        raise RuntimeError("private native products path did not resolve exactly")

    execution = runner.run_shake(
        GLOBAL_EVENT_ID,
        log_file=paths.event_log_file(GLOBAL_EVENT_ID),
        env=context.environment,
    )
    if execution.exit_code != 0 or execution.signal is not None:
        raise RuntimeError(
            f"global native command failed: exit={execution.exit_code} "
            f"signal={execution.signal}"
        )
    native_products = paths.event_native_products_dir(GLOBAL_EVENT_ID)
    if not native_products.is_dir() or not native_products.is_relative_to(
        paths.products_dir()
    ):
        raise RuntimeError("global native products are outside the isolated tree")

    strec_text = profile.strec_configuration_file.read_text(encoding="utf-8")
    required_strec_values = (
        "folder = /opt/shakemap-support/strec",
        "slabfolder = /opt/shakemap-support/slab2/slabs",
        "dbfile = /opt/shakemap-support/strec/moment_tensors.db",
    )
    for value in required_strec_values:
        if value not in strec_text:
            raise RuntimeError(f"private STREC configuration is missing {value!r}")
    if str(context.profile_directory) in strec_text:
        raise RuntimeError("private STREC configuration retained a calculation path")

    return {
        "command": execution.command,
        "exit_code": execution.exit_code,
        "products": str(native_products),
        "profile": str(profile.profile_directory),
    }


def _regional_check() -> dict[str, object]:
    destination = paths.regional_data_dir() / "romania"
    destination.mkdir(parents=True)
    source_hashes: dict[str, str] = {}
    for filename in native_profile.REGIONAL_CONFIGURATION_FILES:
        source = REGIONAL_SEED / filename
        if not source.is_file() or not os.access(source, os.R_OK):
            raise RuntimeError(f"regional seed is unavailable: {source}")
        shutil.copyfile(source, destination / filename)
        source_hashes[filename] = _sha256(source)

    context = _prepare_context(REGIONAL_EVENT_ID, "romania")
    profile = native_profile.materialize_native_profile(context)
    destination_hashes = {
        filename: _sha256(profile.configuration_directory / filename)
        for filename in native_profile.REGIONAL_CONFIGURATION_FILES
    }
    if destination_hashes != source_hashes:
        raise RuntimeError("regional private files differ from their exact sources")
    return {
        "source": str(destination),
        "configuration_hashes": destination_hashes,
        "native_command_executed": False,
    }


def main() -> int:
    if not CHECK_ROOT.is_dir():
        raise RuntimeError(f"container check root is missing: {CHECK_ROOT}")
    if not FIXTURE_FILE.is_file():
        raise RuntimeError(f"container check fixture is missing: {FIXTURE_FILE}")
    if (RUNTIME_ROOT / "shakemap").exists():
        raise RuntimeError("isolated container-check runtime already exists")

    _configure_runtime()
    mounted_state = _link_global_data()
    global_result = _global_check()
    regional_result = _regional_check()
    for source, before in mounted_state.items():
        after = source.stat()
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_mode != before.st_mode
        ):
            raise RuntimeError(f"mounted global source changed during check: {source}")
    print(
        json.dumps(
            {
                "global": global_result,
                "regional": regional_result,
                "mounted_data_unchanged": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
