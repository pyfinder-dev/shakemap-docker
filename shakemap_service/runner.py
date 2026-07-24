# -*- coding: utf-8 -*-
"""Private-profile execution directly in the authoritative calculation folder."""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from . import paths
from .build_identity import service_identity
from .config import settings
from .status import (
    RequestStatus,
    _fsync_directory,
    _record_to_dict,
    transition_to_failed,
    update_status,
    write_json_atomic,
)

logger = logging.getLogger(__name__)


class ShakeError(RuntimeError):
    pass


class CalculationExistsError(RuntimeError):
    pass


@dataclass
class ExecutionResult:
    command: list[str]
    exit_code: int
    pid: int
    started_at: str
    completed_at: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def private_profile_data_root(event_id: str) -> Path:
    """Every private profile maps ShakeMap event data directly to products/."""
    return paths.products_dir()


def _copy_request_snapshot(record: RequestStatus, destination: Path) -> None:
    source = paths.queue_request_dir(record.sequence)
    if not source.is_dir():
        raise FileNotFoundError(
            f"queued request snapshot is missing for sequence {record.sequence}: {source}"
        )
    shutil.copytree(source, destination)


def _write_private_profile(
    record: RequestStatus,
    calculation_root: Path,
    final_root: Path,
) -> dict[str, Any]:
    raise ShakeError(
        "effective ShakeMap configuration resolution is not implemented; "
        "managed calculation materialization remains disabled"
    )


def materialize_calculation(record: RequestStatus) -> Path:
    """Atomically publish a clean calculation folder before native execution.

    Native work is performed only after the prepared folder is renamed to
    ``products/<event_id>``. No working calculation is copied for publication.
    """
    products = paths.products_dir()
    products.mkdir(parents=True, exist_ok=True)
    target = paths.event_products_dir(record.event_id)
    if target.exists():
        raise CalculationExistsError(
            f"calculation already exists and was preserved: {target}; "
            "recalculation archival is not implemented in this milestone"
        )

    temporary = Path(tempfile.mkdtemp(
        dir=products,
        prefix=f".{record.event_id}.preparing-{uuid.uuid4().hex}-",
    ))
    try:
        _copy_request_snapshot(record, temporary / "request")
        (temporary / "current").mkdir()
        for request_file in (temporary / "request").iterdir():
            if request_file.is_file():
                shutil.copy2(request_file, temporary / "current" / request_file.name)
        (temporary / "logs").mkdir()
        profile = _write_private_profile(record, temporary, target)
        metadata = {
            "schema_version": 1,
            "kind": record.kind,
            "event_id": record.event_id,
            "queue_sequence": record.sequence,
            "created_at": _now_iso(),
            "requested_region": record.requested_region,
            "effective_region": record.effective_region,
            "module_plan": record.module_plan,
            "private_profile": profile,
            "mounted_paths": record.mounted_paths,
            "software_identity": service_identity(),
            "success_gate": {
                "implemented": False,
                "reason": "authoritative operational success semantics are Milestone 5",
            },
        }
        write_json_atomic(temporary / "metadata.json", metadata)
        write_json_atomic(temporary / "status.json", _record_to_dict(record))
        os.rename(temporary, target)
        _fsync_directory(products)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return target


def execution_environment(event_id: str) -> dict[str, str]:
    profile = paths.event_profile_dir(event_id)
    home = profile / "home"
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "MPLCONFIGDIR": str(home / ".config/matplotlib"),
        "TMPDIR": str(profile / "tmp"),
    }


def run_shake(
    event_id: str,
    modules: Sequence[str],
    *,
    log_file: Path,
    env: dict[str, str],
    on_started: Optional[Callable[[int, list[str], str], None]] = None,
) -> ExecutionResult:
    command = ["shake", event_id, *modules]
    started_at = _now_iso()
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with log_file.open("w", encoding="utf-8") as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            env=env,
            text=True,
        )
        if on_started is not None:
            on_started(process.pid, command, started_at)
        exit_code = process.wait()
    completed_at = _now_iso()
    return ExecutionResult(
        command=command,
        exit_code=exit_code,
        pid=process.pid,
        started_at=started_at,
        completed_at=completed_at,
    )


def run_shake_for_event(record: RequestStatus) -> str:
    """Execute directly in products/<event_id>/current without publication copy.

    The public worker remains disabled. If this internal function is invoked,
    native exit zero is deliberately not promoted to SUCCESS before Milestone 5
    defines and verifies the authoritative operational gate.
    """
    modules = record.module_plan or settings.shakemap_modules.split()
    record = update_status(record.sequence, module_plan=list(modules))

    try:
        materialize_calculation(record)
    except CalculationExistsError as exc:
        transition_to_failed(
            record.sequence,
            str(exc),
            code="existing_calculation_preserved",
        )
        return "failed"
    except Exception as exc:
        transition_to_failed(
            record.sequence,
            f"Calculation materialization failed: {type(exc).__name__}: {exc}",
            code="materialization_failed",
        )
        return "failed"

    def child_started(pid: int, command: list[str], started_at: str) -> None:
        update_status(
            record.sequence,
            module_plan=list(modules),
            current_child={
                "pid": pid,
                "command": command,
                "started_at": started_at,
                "exit_code": None,
                "signal": None,
            },
        )

    try:
        result = run_shake(
            record.event_id,
            modules,
            log_file=paths.event_log_file(record.event_id),
            env=execution_environment(record.event_id),
            on_started=child_started,
        )
    except Exception as exc:
        transition_to_failed(
            record.sequence,
            f"ShakeMap invocation failed: {type(exc).__name__}: {exc}",
            code="native_invocation_failed",
        )
        return "failed"

    child = {
        "pid": result.pid,
        "command": result.command,
        "started_at": result.started_at,
        "completed_at": result.completed_at,
        "exit_code": result.exit_code,
        "signal": -result.exit_code if result.exit_code < 0 else None,
    }
    if result.exit_code != 0:
        transition_to_failed(
            record.sequence,
            f"ShakeMap exited with code {result.exit_code}",
            code="native_exit_nonzero",
            current_child=child,
        )
        return "failed"

    transition_to_failed(
        record.sequence,
        "Native execution exited zero, but the Milestone 5 operational "
        "success gate is not implemented; outputs were preserved",
        code="success_gate_not_implemented",
        current_child=child,
    )
    return "native_completed_success_gate_pending"
