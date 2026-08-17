"""Reconcile interrupted durable calculations before the service starts."""
from __future__ import annotations

from . import recalculation, status


class StartupRecoveryError(RuntimeError):
    """Durable startup state could not be made truthful."""


def _validated_records() -> list[status.CalculationRecord]:
    queue_records, queue_errors = status.scan_queue_records()
    current_records, current_errors = status.scan_current_records()
    problems = [f"queue record {entry}" for entry, _ in queue_errors]
    problems.extend(f"current record {entry}" for entry, _ in current_errors)
    if problems:
        raise StartupRecoveryError(
            "malformed durable state prevents startup: " + ", ".join(problems)
        )

    records = [*queue_records, *current_records]
    seen_sequences: dict[int, str] = {}
    for record in records:
        previous_event_id = seen_sequences.get(record.internal_sequence)
        if previous_event_id is not None:
            raise StartupRecoveryError(
                f"duplicate durable internal sequence prevents startup: "
                f"{record.internal_sequence} ({previous_event_id!r}, {record.event_id!r})"
            )
        seen_sequences[record.internal_sequence] = record.event_id
    return records


def recover_interrupted_calculations() -> tuple[tuple[str, int], ...]:
    """Fail stale running calculations without executing or retrying them."""
    running = [
        record for record in _validated_records()
        if record.status == status.LifecycleState.RUNNING.value
    ]
    running.sort(key=lambda record: record.internal_sequence)
    recovered: list[tuple[str, int]] = []
    for record in running:
        try:
            changed = recalculation.recover_stale_running_calculation(
                record.event_id, record.internal_sequence
            )
        except Exception as exc:
            raise StartupRecoveryError(
                "interrupted calculation could not be recovered: "
                f"{record.internal_sequence} ({record.event_id!r})"
            ) from exc
        if changed:
            recovered.append((record.event_id, record.internal_sequence))

    remaining = [
        record
        for record in _validated_records()
        if record.status == status.LifecycleState.RUNNING.value
    ]
    if remaining:
        remaining.sort(key=lambda record: record.internal_sequence)
        identities = ", ".join(
            f"{item.internal_sequence} ({item.event_id!r})" for item in remaining
        )
        raise StartupRecoveryError(
            f"stale running calculations remain after recovery: {identities}"
        )
    return tuple(recovered)
