# -*- coding: utf-8 -*-
"""Bounded admission and reservation for queued calculations."""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Condition, RLock
from typing import Callable, Optional

from . import paths, recalculation, status
from .config import Settings, settings
from .queue import MalformedRecord, discover_queue


CalculationCallback = Callable[[status.CalculationRecord], object]


@dataclass(frozen=True)
class SchedulerError:
    sequence: int
    event_id: str
    message: str


class Scheduler:
    """Admit eligible queue entries to an injected calculation callback."""

    def __init__(
        self,
        calculation_callback: CalculationCallback,
        *,
        service_settings: Optional[Settings] = None,
    ) -> None:
        if not callable(calculation_callback):
            raise ValueError("calculation_callback must be callable")
        configured = settings if service_settings is None else service_settings
        self._calculation_callback = calculation_callback
        self._max_concurrent = configured.max_concurrent
        self._executor = ThreadPoolExecutor(
            max_workers=self._max_concurrent,
            thread_name_prefix="shakemap-calculation",
        )
        self._lock = RLock()
        self._idle = Condition(self._lock)
        self._worker_slots: dict[int, str] = {}
        self._event_reservations: dict[str, int] = {}
        self._errors: list[SchedulerError] = []
        self._closed = False

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def active_count(self) -> int:
        with self._lock:
            return len(self._worker_slots)

    @property
    def reserved_event_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._event_reservations)

    @property
    def errors(self) -> tuple[SchedulerError, ...]:
        with self._lock:
            return tuple(self._errors)

    def tick(self) -> tuple[status.CalculationRecord, ...]:
        """Start the oldest eligible records up to the configured capacity."""
        with self._idle:
            if self._closed:
                raise RuntimeError("scheduler is shut down")
            queued, malformed = discover_queue()
            barrier = _first_malformed_sequence(malformed)
            started: list[status.CalculationRecord] = []
            seen_event_ids: set[str] = set()

            for record in queued:
                if len(self._worker_slots) >= self._max_concurrent:
                    break
                if barrier is not None and record.internal_sequence > barrier:
                    break
                if record.event_id in seen_event_ids:
                    continue
                seen_event_ids.add(record.event_id)
                if record.event_id in self._event_reservations:
                    continue
                try:
                    transaction_present = recalculation.current_transaction_present(
                        record.event_id
                    )
                except Exception as exc:
                    self._record_error(record, exc)
                    continue
                if transaction_present:
                    continue

                self._worker_slots[record.internal_sequence] = record.event_id
                self._event_reservations[record.event_id] = (
                    record.internal_sequence
                )
                try:
                    running = status.transition_to_running(record.internal_sequence)
                except Exception as exc:
                    self._finish_without_callback(record, exc)
                    continue

                try:
                    self._executor.submit(self._run_calculation, running)
                except Exception as exc:
                    self._finish_without_callback(running, exc)
                    continue
                started.append(running)

            return tuple(started)

    def wait_until_idle(self, timeout: Optional[float] = None) -> bool:
        """Wait until no calculation occupies worker capacity.

        An event with unresolved terminal transaction state can remain reserved
        after this method returns.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._idle:
            while self._worker_slots:
                if deadline is None:
                    self._idle.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
            return True

    def shutdown(self, *, wait: bool = True) -> None:
        with self._idle:
            self._closed = True
        self._executor.shutdown(wait=wait)

    def _run_calculation(self, record: status.CalculationRecord) -> None:
        callback_error: Optional[Exception] = None
        try:
            self._calculation_callback(record)
        except Exception as exc:
            callback_error = exc

        if callback_error is not None:
            try:
                reconciled = recalculation.reconcile_transaction(
                    record.event_id,
                    record.internal_sequence,
                )
            except Exception as exc:
                self._record_error(record, exc)
                self._drop_capacity_for_terminal_current(record)
                return
            if reconciled:
                try:
                    location, _ = self._verify_terminal(record)
                    if location != "current":
                        raise RuntimeError(
                            "reconciled calculation is not the current record"
                        )
                except Exception as exc:
                    self._record_error(record, exc)
                    return
                try:
                    recalculation.finalize_transaction(record.event_id)
                except Exception as exc:
                    self._record_error(record, exc)
                    self._retain_event_without_capacity(record)
                    return
                self._release(record)
                return

        try:
            location, _ = self._ensure_terminal(record, callback_error)
            if location == "current":
                try:
                    recalculation.finalize_transaction(record.event_id)
                except Exception:
                    self._retain_event_without_capacity(record)
                    raise
        except Exception as exc:
            self._record_error(record, exc)
            return
        self._release(record)

    def _finish_without_callback(
        self,
        record: status.CalculationRecord,
        error: Exception,
    ) -> None:
        try:
            target = _read_authoritative_record(record)
            if target is None or target[1].status == status.LifecycleState.QUEUED.value:
                self._record_error(record, error)
                self._release(record)
                return
            self._ensure_terminal(record, error)
        except Exception as finalization_error:
            self._record_error(record, finalization_error)
            return
        self._release(record)

    def _ensure_terminal(
        self,
        record: status.CalculationRecord,
        callback_error: Optional[Exception],
    ) -> tuple[str, status.CalculationRecord]:
        target = _read_authoritative_record(record)
        if target is None:
            raise RuntimeError("calculation record is missing after callback")
        location, current = target
        lifecycle = status.LifecycleState(current.status)
        if lifecycle in status.TERMINAL_STATES:
            return location, current
        if lifecycle != status.LifecycleState.RUNNING:
            raise RuntimeError(
                f"calculation record has unexpected status {lifecycle.value}"
            )

        if callback_error is None:
            code = "scheduler_callback_incomplete"
            message = "calculation callback returned while the record was RUNNING"
        else:
            code = "scheduler_callback_failed"
            message = (
                "calculation callback failed: "
                f"{type(callback_error).__name__}: {callback_error}"
            )
        if location == "queue":
            failed = status.transition_to_failed(
                record.internal_sequence,
                message,
                code=code,
            )
        else:
            failed = status.transition_current_record(
                record.event_id,
                status.LifecycleState.FAILED,
                failure={"code": code, "message": message},
                service_outcome={"completed": True, "successful": False},
            )
        return location, failed

    def _verify_terminal(
        self,
        record: status.CalculationRecord,
    ) -> tuple[str, status.CalculationRecord]:
        target = _read_authoritative_record(record)
        if target is None:
            raise RuntimeError("calculation record is missing after reconciliation")
        location, current = target
        if status.LifecycleState(current.status) not in status.TERMINAL_STATES:
            raise RuntimeError(
                "reconciliation did not leave a terminal calculation record"
            )
        return location, current

    def _record_error(
        self,
        record: status.CalculationRecord,
        error: Exception,
    ) -> None:
        with self._idle:
            self._errors.append(
                SchedulerError(
                    sequence=record.internal_sequence,
                    event_id=record.event_id,
                    message=f"{type(error).__name__}: {error}",
                )
            )

    def _release(self, record: status.CalculationRecord) -> None:
        with self._idle:
            self._worker_slots.pop(record.internal_sequence, None)
            if (
                self._event_reservations.get(record.event_id)
                == record.internal_sequence
            ):
                self._event_reservations.pop(record.event_id)
            self._idle.notify_all()

    def _retain_event_without_capacity(
        self,
        record: status.CalculationRecord,
    ) -> None:
        with self._idle:
            self._worker_slots.pop(record.internal_sequence, None)
            self._idle.notify_all()

    def _drop_capacity_for_terminal_current(
        self,
        record: status.CalculationRecord,
    ) -> None:
        try:
            target = _read_authoritative_record(record)
        except Exception:
            return
        if target is None:
            return
        location, current = target
        if (
            location == "current"
            and status.LifecycleState(current.status) in status.TERMINAL_STATES
        ):
            self._retain_event_without_capacity(record)


def _first_malformed_sequence(
    malformed: list[MalformedRecord],
) -> Optional[int]:
    sequences: list[int] = []
    for item in malformed:
        try:
            sequences.append(paths.parse_queue_entry_name(item.entry))
        except ValueError:
            continue
    return min(sequences, default=None)


def _read_authoritative_record(
    expected: status.CalculationRecord,
) -> Optional[tuple[str, status.CalculationRecord]]:
    queued = status.read_status(expected.internal_sequence)
    if queued is not None:
        if queued.event_id != expected.event_id:
            raise ValueError("queued calculation identity changed while reserved")
        return "queue", queued

    current = status.read_current_record(expected.event_id)
    if current is None:
        return None
    if current.internal_sequence != expected.internal_sequence:
        raise ValueError("current calculation identity changed while reserved")
    return "current", current
