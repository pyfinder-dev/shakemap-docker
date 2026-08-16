"""Exercise complete and concurrent calculations in the supported container."""
from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import threading
import time
from pathlib import Path
from typing import Callable

from shakemap_service import (
    calculation,
    paths,
    provenance,
    required_products,
    status,
    worker,
)
from shakemap_service.config import MODULE_PLAN, Settings
from shakemap_service.scheduler import Scheduler
from shakemap_service.submission import Upload, accept_request


CHECK_ROOT = Path("/tmp/m5k-check")
RUNTIME_ROOT = CHECK_ROOT / "runtime"
FIXTURE_ROOT = CHECK_ROOT / "fixture"
MOUNTED_DATA_ROOT = Path("/home/sysop/runtime/shakemap/data")
GLOBAL_DATA = (
    Path("global/vs30/global_vs30.grd"),
    Path("global/topo/topo_30sec.grd"),
)
EXPECTED_REQUIRED = (
    "shake_result.hdf",
    "intensity.jpg",
    "pga.jpg",
    "pgv.jpg",
    "psa0p3.jpg",
    "psa1p0.jpg",
    "psa3p0.jpg",
)
SINGLE_EVENT = "m5k-single-success"
CONCURRENT_EVENTS = ("m5k-concurrent-a", "m5k-concurrent-b")
SERIAL_EVENT = "m5k-serialized"
SETTINGS = Settings(
    runtime_root=str(RUNTIME_ROOT),
    shared_runtime_root="/operator/m5k-runtime",
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _configure_runtime() -> None:
    paths.settings = SETTINGS
    status.settings = SETTINGS
    required_products.settings = SETTINGS
    provenance.settings = SETTINGS


def _metadata(path: Path) -> dict[str, object]:
    details = path.stat()
    return {
        "path": str(path),
        "device": details.st_dev,
        "inode": details.st_ino,
        "mode": stat.S_IMODE(details.st_mode),
        "size": details.st_size,
        "mtime_ns": details.st_mtime_ns,
        "ctime_ns": details.st_ctime_ns,
    }


def _link_global_data() -> dict[str, dict[str, object]]:
    before: dict[str, dict[str, object]] = {}
    for relative in GLOBAL_DATA:
        source = MOUNTED_DATA_ROOT / relative
        _require(
            source.is_file() and os.access(source, os.R_OK),
            f"mounted global source is unavailable: {source}",
        )
        before[str(source)] = _metadata(source)
        destination = paths.shakemap_data_dir() / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source)
    return before


def _accept(event_id: str):
    streams = []
    try:
        for name in ("event.xml", "event_dat.xml"):
            stream = (FIXTURE_ROOT / name).open("rb")
            streams.append(stream)
        return accept_request(
            event_id,
            [
                Upload("event.xml", streams[0]),
                Upload("event_dat.xml", streams[1]),
            ],
            configuration="global",
        )
    finally:
        for stream in streams:
            stream.close()


def _run_scheduler(
    event_ids: tuple[str, ...],
    callback: Callable[[status.CalculationRecord], object],
    *,
    timeout: float = 600,
) -> tuple[status.CalculationRecord, ...]:
    accepted = tuple(_accept(event_id) for event_id in event_ids)
    scheduler = Scheduler(callback, service_settings=SETTINGS)
    try:
        started = scheduler.tick()
        _require(
            [item.internal_sequence for item in started]
            == [item.internal_sequence for item in accepted],
            "scheduler did not start the expected accepted records",
        )
        _require(
            scheduler.wait_until_idle(timeout=timeout),
            "scheduler did not become idle before timeout",
        )
        _require(not scheduler.errors, f"scheduler recorded errors: {scheduler.errors}")
    finally:
        scheduler.shutdown()
    records = tuple(status.read_current_record(event_id) for event_id in event_ids)
    _require(all(record is not None for record in records), "current record is missing")
    return records  # type: ignore[return-value]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_inventory(event_id: str) -> list[dict[str, object]]:
    root = paths.event_native_products_dir(event_id)
    inventory = []
    for product in sorted(root.rglob("*")):
        details = product.lstat()
        if stat.S_ISREG(details.st_mode):
            inventory.append(
                {
                    "path": product.relative_to(root).as_posix(),
                    "size": details.st_size,
                    "sha256": _sha256(product),
                }
            )
    return inventory


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(payload, dict), f"expected JSON object: {path}")
    return payload


def _verify_single_success(record: status.CalculationRecord) -> dict[str, object]:
    _require(record.status == status.LifecycleState.SUCCESS.value, "single run failed")
    _require(
        record.native_outcome == {"started": True, "exit_code": 0, "signal": None},
        f"unexpected native outcome: {record.native_outcome!r}",
    )
    _require(
        record.service_outcome == {"completed": True, "successful": True},
        f"unexpected service outcome: {record.service_outcome!r}",
    )
    _require(record.failure is None, f"success retained failure: {record.failure!r}")

    manifest = _read_json(paths.event_manifest_file(record.event_id))
    _require(
        manifest.get("event_id") == record.event_id
        and manifest.get("internal_sequence") == record.internal_sequence,
        "product manifest identity differs",
    )
    _require(manifest.get("partial") is False, "product manifest is partial")
    required = manifest.get("required_products")
    _require(isinstance(required, dict), "required-product evidence is absent")
    _require(required.get("paths") == list(EXPECTED_REQUIRED), "required paths differ")
    _require(required.get("source") == "derived", "required source is not derived")
    _require(required.get("passed") is True, "required products did not all pass")
    checks = required.get("checks")
    _require(isinstance(checks, list), "generic product checks are absent")
    _require(
        [item.get("path") for item in checks] == list(EXPECTED_REQUIRED),
        "generic checks are not in resolved order",
    )
    _require(
        all(
            item.get("passed") is True
            and item.get("reason") == "generic checks passed"
            and isinstance(item.get("size"), int)
            and item.get("size") > 0
            for item in checks
        ),
        "generic required-product evidence is incomplete",
    )
    actual_inventory = _native_inventory(record.event_id)
    _require(manifest.get("products") == actual_inventory, "manifest inventory differs")

    provenance_record = _read_json(paths.event_provenance_file(record.event_id))
    _require(
        provenance_record.get("event_id") == record.event_id
        and provenance_record.get("internal_sequence") == record.internal_sequence,
        "provenance identity differs",
    )
    native_execution = provenance_record.get("native_execution")
    _require(isinstance(native_execution, dict), "native execution is absent")
    expected_command = ["shake", record.event_id, *MODULE_PLAN]
    _require(native_execution.get("command") == expected_command, "command differs")
    _require(
        native_execution.get("exit_code") == 0
        and native_execution.get("signal") is None
        and isinstance(native_execution.get("pid"), int),
        "native execution outcome differs",
    )
    timestamps = provenance_record.get("timestamps")
    _require(isinstance(timestamps, dict), "provenance timestamps are absent")
    _require(
        timestamps.get("terminal_at") == record.timestamps["completed_at"],
        "provenance and status terminal timestamps differ",
    )
    _require(
        provenance_record.get("required_products")
        == {"paths": list(EXPECTED_REQUIRED), "source": "derived"},
        "provenance required-product resolution differs",
    )
    _require(
        provenance_record.get("module_plan") == list(MODULE_PLAN),
        "provenance module plan differs",
    )
    _require(
        provenance_record.get("outcomes")
        == {
            "native": record.native_outcome,
            "service": record.service_outcome,
        },
        "provenance outcomes differ from terminal status",
    )
    request = provenance_record.get("request")
    configuration = provenance_record.get("configuration")
    _require(
        isinstance(request, dict)
        and request.get("configuration") == "global"
        and isinstance(configuration, dict)
        and configuration.get("selected") == "global",
        "provenance selected configuration differs",
    )

    for log_path in (
        paths.event_service_log_file(record.event_id),
        paths.event_log_file(record.event_id),
    ):
        details = log_path.lstat()
        _require(
            stat.S_ISREG(details.st_mode) and details.st_size > 0,
            f"required log is not regular and nonempty: {log_path}",
        )
        with log_path.open("rb") as stream:
            _require(bool(stream.read(1)), f"required log is unreadable: {log_path}")

    shared_service = Path(SETTINGS.shared_service_root) / ".service" / "events" / record.event_id
    expected_shared = {
        "input": str(Path(SETTINGS.shared_service_root) / "data" / "inputs" / record.event_id),
        "products": str(Path(SETTINGS.shared_service_root) / "products" / record.event_id / "current" / "products"),
        "provenance": str(shared_service / "provenance.json"),
        "product_manifest": str(shared_service / "product-manifest.json"),
        "service_log": str(shared_service / "logs" / "service.log"),
        "shake_log": str(shared_service / "logs" / "shake.log"),
    }
    _require(record.shared_paths == expected_shared, "shared paths differ")
    _require(
        not (paths.event_service_dir(record.event_id) / "transaction.json").exists(),
        "completed recalculation transaction remains",
    )
    return {
        "event_id": record.event_id,
        "internal_sequence": record.internal_sequence,
        "status": record.status,
        "native_outcome": record.native_outcome,
        "service_outcome": record.service_outcome,
        "command": native_execution["command"],
        "pid": native_execution["pid"],
        "started_at": native_execution["started_at"],
        "completed_at": native_execution["completed_at"],
        "terminal_at": record.timestamps["completed_at"],
        "required_products": required,
        "inventory_count": len(actual_inventory),
        "inventory": actual_inventory,
        "shared_paths": record.shared_paths,
        "logs_nonempty": True,
        "transaction_removed": True,
    }


def _different_id_concurrency() -> dict[str, object]:
    real_run = calculation.runner.run_shake
    lock = threading.Lock()
    spans: dict[str, dict[str, object]] = {}

    def observed_run(
        event_id: str,
        *,
        log_file: Path,
        env: dict[str, str],
        on_started=None,
    ):
        def observed_started(pid: int, command: list[str], started_at: str) -> None:
            with lock:
                spans[event_id] = {
                    "pid": pid,
                    "command": list(command),
                    "runner_started_at": started_at,
                    "overlap_started_ns": time.monotonic_ns(),
                }
            if on_started is not None:
                on_started(pid, command, started_at)

        result = real_run(
            event_id,
            log_file=log_file,
            env=env,
            on_started=observed_started,
        )
        with lock:
            spans[event_id]["overlap_completed_ns"] = time.monotonic_ns()
            spans[event_id]["runner_completed_at"] = result.completed_at
        return result

    calculation.runner.run_shake = observed_run
    try:
        records = _run_scheduler(CONCURRENT_EVENTS, worker.execute_shakemap)
    finally:
        calculation.runner.run_shake = real_run

    _require(set(spans) == set(CONCURRENT_EVENTS), "native span evidence is incomplete")
    starts = [int(spans[event_id]["overlap_started_ns"]) for event_id in CONCURRENT_EVENTS]
    ends = [int(spans[event_id]["overlap_completed_ns"]) for event_id in CONCURRENT_EVENTS]
    _require(max(starts) < min(ends), "different-ID native processes did not overlap")
    _require(
        len({spans[event_id]["pid"] for event_id in CONCURRENT_EVENTS}) == 2,
        "different-ID runs did not have distinct native processes",
    )
    for record in records:
        _require(record.status == "SUCCESS", f"concurrent run failed: {record.event_id}")
        _require(record.native_outcome == {"started": True, "exit_code": 0, "signal": None}, "concurrent native outcome differs")
        _require(not (paths.event_service_dir(record.event_id) / "transaction.json").exists(), "concurrent transaction remains")
    return {
        "default_capacity": SETTINGS.max_concurrent,
        "event_ids": list(CONCURRENT_EVENTS),
        "overlap": True,
        "spans": spans,
        "terminal": [
            {
                "event_id": record.event_id,
                "internal_sequence": record.internal_sequence,
                "status": record.status,
                "native_outcome": record.native_outcome,
            }
            for record in records
        ],
    }


def _same_id_serialization() -> dict[str, object]:
    accepted = (_accept(SERIAL_EVENT), _accept(SERIAL_EVENT))
    entered = threading.Event()
    release = threading.Event()
    lock = threading.Lock()
    callback_order: list[int] = []
    active = 0
    peak = 0

    def controlled(record: status.CalculationRecord) -> None:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            callback_order.append(record.internal_sequence)
            if len(callback_order) == 1:
                entered.set()
        if len(callback_order) == 1 and not release.wait(timeout=30):
            raise RuntimeError("controlled same-ID callback was not released")
        status.transition_status(
            record.internal_sequence,
            status.LifecycleState.FAILED,
            failure={"code": "controlled_completion", "message": "scheduler-only check"},
            service_outcome={"completed": True, "successful": False},
        )
        with lock:
            active -= 1

    scheduler = Scheduler(controlled, service_settings=SETTINGS)
    try:
        first_started = scheduler.tick()
        _require(entered.wait(timeout=30), "first same-ID callback did not start")
        _require(
            [item.internal_sequence for item in first_started]
            == [accepted[0].internal_sequence],
            "same-ID scheduler started more than the first record",
        )
        second_waiting = status.read_status(accepted[1].internal_sequence)
        _require(second_waiting is not None and second_waiting.status == "QUEUED", "second same-ID record did not remain queued")
        release.set()
        _require(scheduler.wait_until_idle(timeout=30), "first same-ID callback did not finish")
        second_started = scheduler.tick()
        _require(
            [item.internal_sequence for item in second_started]
            == [accepted[1].internal_sequence],
            "second same-ID record did not start in order",
        )
        _require(scheduler.wait_until_idle(timeout=30), "second same-ID callback did not finish")
        _require(not scheduler.errors, f"same-ID scheduler errors: {scheduler.errors}")
    finally:
        release.set()
        scheduler.shutdown()
    expected_order = [item.internal_sequence for item in accepted]
    _require(callback_order == expected_order, "same-ID callback order differs")
    _require(peak == 1, "same-ID callbacks overlapped")
    return {
        "event_id": SERIAL_EVENT,
        "accepted_order": expected_order,
        "callback_order": callback_order,
        "peak_callbacks": peak,
        "native_runs": 0,
    }


def main() -> int:
    _require(CHECK_ROOT.is_dir(), f"container check root is missing: {CHECK_ROOT}")
    for name in ("event.xml", "event_dat.xml"):
        _require((FIXTURE_ROOT / name).is_file(), f"fixture is missing: {name}")
    _require(not (RUNTIME_ROOT / "shakemap").exists(), "isolated runtime already exists")
    _require(SETTINGS.max_concurrent == 10, "default concurrency changed")
    _configure_runtime()
    mounted_before = _link_global_data()

    single_record = _run_scheduler((SINGLE_EVENT,), worker.execute_shakemap)[0]
    single = _verify_single_success(single_record)
    concurrent = _different_id_concurrency()
    serialized = _same_id_serialization()

    mounted_after = {
        path: _metadata(Path(path))
        for path in mounted_before
    }
    _require(mounted_after == mounted_before, "mounted global source metadata changed")
    print(
        json.dumps(
            {
                "single_success": single,
                "different_id_concurrency": concurrent,
                "same_id_serialization": serialized,
                "mounted_data": {
                    "before": mounted_before,
                    "after": mounted_after,
                    "unchanged": True,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
