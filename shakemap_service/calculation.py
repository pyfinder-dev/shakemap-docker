"""Compose one promoted ShakeMap calculation from service-owned phases."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from . import (
    calculation_finalization,
    calculation_log,
    native_context,
    native_profile,
    paths,
    product_manifest,
    product_validation,
    provenance,
    recalculation,
    required_products,
    runner,
    status,
)
from .directory_access import open_service_directory


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _error_text(error: BaseException) -> str:
    detail = str(error)
    if detail:
        return detail
    return type(error).__name__


def _current_promoted_record(
    supplied: status.CalculationRecord,
) -> status.CalculationRecord:
    current = status.read_current_record(supplied.event_id)
    if current is None:
        raise FileNotFoundError(
            f"current calculation record for {supplied.event_id!r} does not exist"
        )
    if (
        current.event_id != supplied.event_id
        or current.internal_sequence != supplied.internal_sequence
    ):
        raise ValueError("promoted calculation record identity does not match")
    if current.status != status.LifecycleState.RUNNING.value:
        raise ValueError("promoted calculation record must be RUNNING")
    return current


def _set_phase(
    record: status.CalculationRecord,
    phase: str,
) -> status.CalculationRecord:
    progress = dict(record.progress)
    progress.update(
        {
            "phase": phase,
            "phase_started_at": _now_iso(),
            "current_module": None,
            "completed_modules": [],
        }
    )
    return status.update_current_record(record.event_id, progress=progress)


def _append_log(
    record: status.CalculationRecord,
    *,
    phase: str,
    severity: str,
    message: str,
) -> None:
    calculation_log.append_service_log(
        record,
        phase=phase,
        severity=severity,
        message=message,
    )


def _precreate_shake_log(record: status.CalculationRecord) -> Path:
    logs = open_service_directory(paths.event_logs_dir(record.event_id), create=False)
    descriptor = -1
    try:
        descriptor = os.open(
            "shake.log",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=logs.descriptor,
        )
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.fsync(logs.descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        logs.close()
    return paths.event_log_file(record.event_id)


def _helper_facts(
    helper: native_profile.HelperExecution,
) -> dict[str, object]:
    return {
        "command": list(helper.command),
        "output": helper.output,
        "return_code": helper.return_code,
    }


def _profile_facts(
    materialization: native_profile.NativeProfileMaterialization,
) -> dict[str, object]:
    return {
        "materialized": True,
        "selected_configuration": materialization.selected_configuration,
        "profile_name": materialization.profile_name,
        "source_directory": None
        if materialization.source_directory is None
        else str(materialization.source_directory),
        "profile_helper": _helper_facts(materialization.profile_helper),
        "strec_helper": _helper_facts(materialization.strec_helper),
    }


def _profile_failure_facts(
    selected_configuration: str,
    error: BaseException,
) -> dict[str, object]:
    facts: dict[str, object] = {
        "materialized": False,
        "selected_configuration": selected_configuration,
        "failure": {
            "type": type(error).__name__,
            "message": _error_text(error),
        },
    }
    if isinstance(error, native_profile.NativeProfileError):
        failure = facts["failure"]
        if isinstance(failure, dict):
            failure["stage"] = error.stage
            failure["command"] = (
                None if error.command is None else list(error.command)
            )
            failure["helper"] = (
                None if error.helper is None else _helper_facts(error.helper)
            )
    return facts


def _execution_facts(
    execution: runner.ExecutionResult | None,
    started: Mapping[str, object] | None,
) -> dict[str, object] | None:
    if execution is None:
        return None if started is None else dict(started)
    return {
        "command": list(execution.command),
        "pid": execution.pid,
        "started_at": execution.started_at,
        "completed_at": execution.completed_at,
        "exit_code": execution.exit_code,
        "signal": execution.signal,
    }


def _native_outcome(
    execution: runner.ExecutionResult | None,
) -> dict[str, object] | None:
    if execution is None:
        return None
    return {
        "started": True,
        "exit_code": execution.exit_code,
        "signal": execution.signal,
    }


def _timestamps(
    record: status.CalculationRecord,
    *,
    execution: runner.ExecutionResult | None,
    validated_at: str | None,
    terminal_at: str,
) -> dict[str, str | None]:
    return {
        "accepted_at": record.timestamps["submitted_at"],
        "started_at": record.timestamps["started_at"],
        "native_completed_at": None
        if execution is None
        else execution.completed_at,
        "validated_at": validated_at,
        "terminal_at": terminal_at,
    }


def _provenance_facts(
    record: status.CalculationRecord,
    *,
    configuration_materialization: Mapping[str, object],
    execution: runner.ExecutionResult | None,
    execution_started: Mapping[str, object] | None,
    resolution: required_products.RequiredProductResolution | None,
    validated_at: str | None,
    terminal_at: str,
    failure: Mapping[str, object] | None,
) -> provenance.ProvenanceFacts:
    successful = failure is None
    return provenance.ProvenanceFacts(
        configuration_materialization=configuration_materialization,
        native_execution=_execution_facts(execution, execution_started),
        required_products=resolution,
        native_outcome=_native_outcome(execution),
        service_outcome={"completed": True, "successful": successful},
        warnings=tuple(record.warnings),
        failure=failure,
        timestamps=_timestamps(
            record,
            execution=execution,
            validated_at=validated_at,
            terminal_at=terminal_at,
        ),
    )


def _secondary(
    phase: str,
    code: str,
    error: BaseException,
) -> dict[str, object]:
    return {
        "phase": phase,
        "code": code,
        "message": _error_text(error),
    }


def _finish_failed(
    record: status.CalculationRecord,
    *,
    phase: str,
    code: str,
    message: str,
    configuration_materialization: Mapping[str, object],
    execution: runner.ExecutionResult | None,
    execution_started: Mapping[str, object] | None,
    resolution: required_products.RequiredProductResolution | None,
    validation: product_validation.ProductValidationResult | None,
    validated_at: str | None,
    terminal_at: str | None = None,
    record_failure_log: bool = True,
) -> str:
    if terminal_at is None:
        terminal_at = _now_iso()
    primary = {"phase": phase, "code": code, "message": message}
    secondary: list[dict[str, object]] = []

    if record_failure_log:
        try:
            _append_log(
                record,
                phase=phase,
                severity="ERROR",
                message=message,
            )
        except Exception as error:
            secondary.append(
                _secondary(
                    "record_finalization",
                    "service_log_recording_failed",
                    error,
                )
            )

    try:
        product_manifest.publish_product_manifest(
            record,
            validation,
            primary_reason=message,
        )
    except Exception as error:
        secondary.append(
            _secondary(
                "record_finalization",
                "partial_manifest_failed",
                error,
            )
        )

    failure_facts = dict(primary)
    failure_facts["secondary_evidence"] = list(secondary)
    try:
        provenance.publish_provenance(
            record,
            _provenance_facts(
                record,
                configuration_materialization=configuration_materialization,
                execution=execution,
                execution_started=execution_started,
                resolution=resolution,
                validated_at=validated_at,
                terminal_at=terminal_at,
                failure=failure_facts,
            ),
        )
    except Exception as error:
        secondary.append(
            _secondary(
                "record_finalization",
                "provenance_recording_failed",
                error,
            )
        )

    terminal = calculation_finalization.finalize_failure(
        record,
        terminal_timestamp=terminal_at,
        phase=phase,
        code=code,
        message=message,
        execution=execution,
        secondary_evidence=tuple(secondary),
    )
    recalculation.finalize_transaction(record.event_id)
    return terminal.status


def execute_calculation(
    record: status.CalculationRecord,
    *,
    base_environment: Mapping[str, str],
) -> str:
    """Execute and durably finalize one scheduler-supplied calculation."""
    environment = dict(base_environment)
    recalculation.prepare_calculation(record.internal_sequence)
    current = _current_promoted_record(record)
    selected = current.configuration["selected"]
    configuration_facts: Mapping[str, object] = {
        "materialized": False,
        "selected_configuration": selected,
        "state": "not_attempted",
    }
    execution: runner.ExecutionResult | None = None
    execution_started: dict[str, object] | None = None
    resolution: required_products.RequiredProductResolution | None = None
    validation: product_validation.ProductValidationResult | None = None
    validated_at: str | None = None

    try:
        current = _set_phase(current, "calculation_preparation")
    except Exception as error:
        return _finish_failed(
            current,
            phase="calculation_preparation",
            code="phase_recording_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        _append_log(
            current,
            phase="calculation_preparation",
            severity="INFO",
            message="current calculation trees are prepared",
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="calculation_preparation",
            code="service_log_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            record_failure_log=False,
        )
    for warning in current.warnings:
        try:
            _append_log(
                current,
                phase="calculation_preparation",
                severity="WARNING",
                message=warning,
            )
        except Exception as error:
            return _finish_failed(
                current,
                phase="calculation_preparation",
                code="service_log_failed",
                message=_error_text(error),
                configuration_materialization=configuration_facts,
                execution=execution,
                execution_started=execution_started,
                resolution=resolution,
                validation=validation,
                validated_at=validated_at,
                record_failure_log=False,
            )
    try:
        shake_log = _precreate_shake_log(current)
    except Exception as error:
        return _finish_failed(
            current,
            phase="calculation_preparation",
            code="shake_log_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        context = native_context.prepare_native_context(current, environment)
    except Exception as error:
        return _finish_failed(
            current,
            phase="calculation_preparation",
            code="native_context_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        materialization = native_profile.materialize_native_profile(context)
        configuration_facts = _profile_facts(materialization)
    except Exception as error:
        configuration_facts = _profile_failure_facts(selected, error)
        return _finish_failed(
            current,
            phase="calculation_preparation",
            code="configuration_materialization_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        _append_log(
            current,
            phase="calculation_preparation",
            severity="INFO",
            message=f"selected configuration {selected!r} is materialized",
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="calculation_preparation",
            code="service_log_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            record_failure_log=False,
        )

    try:
        current = _set_phase(current, "native_execution")
    except Exception as error:
        return _finish_failed(
            current,
            phase="native_execution",
            code="phase_recording_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        _append_log(
            current,
            phase="native_execution",
            severity="INFO",
            message="starting the fixed native ShakeMap command",
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="native_execution",
            code="service_log_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            record_failure_log=False,
        )

    def on_started(pid: int, command: list[str], started_at: str) -> None:
        nonlocal current, execution_started
        execution_started = {
            "command": list(command),
            "pid": pid,
            "started_at": started_at,
            "completed_at": None,
            "exit_code": None,
            "signal": None,
        }
        timestamps = dict(current.timestamps)
        timestamps["native_started_at"] = started_at
        current = status.update_current_record(
            current.event_id,
            timestamps=timestamps,
            native_outcome={
                "started": True,
                "exit_code": None,
                "signal": None,
            },
        )

    try:
        execution = runner.run_shake(
            current.event_id,
            log_file=shake_log,
            env=context.environment,
            on_started=on_started,
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="native_execution",
            code="native_invocation_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        timestamps = dict(current.timestamps)
        timestamps["native_finished_at"] = execution.completed_at
        current = status.update_current_record(
            current.event_id,
            timestamps=timestamps,
            native_outcome=_native_outcome(execution),
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="native_execution",
            code="native_result_recording_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        _append_log(
            current,
            phase="native_execution",
            severity="INFO",
            message=(
                "native command completed with "
                f"exit_code={execution.exit_code!r}, signal={execution.signal!r}"
            ),
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="native_execution",
            code="service_log_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            record_failure_log=False,
        )
    if execution.signal is not None:
        return _finish_failed(
            current,
            phase="native_execution",
            code="native_signal",
            message=f"ShakeMap terminated by signal {execution.signal}",
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    if execution.exit_code != 0:
        return _finish_failed(
            current,
            phase="native_execution",
            code="native_exit",
            message=f"ShakeMap exited with code {execution.exit_code}",
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )

    try:
        current = _set_phase(current, "product_validation")
    except Exception as error:
        return _finish_failed(
            current,
            phase="product_validation",
            code="phase_recording_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        _append_log(
            current,
            phase="product_validation",
            severity="INFO",
            message="resolving and validating required native products",
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="product_validation",
            code="service_log_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            record_failure_log=False,
        )
    products_directory = paths.event_native_products_dir(current.event_id)
    try:
        resolution = required_products.resolve_required_products(products_directory)
    except Exception as error:
        return _finish_failed(
            current,
            phase="product_validation",
            code="required_product_resolution_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        validation = product_validation.validate_required_products(
            products_directory,
            resolution,
        )
        validated_at = _now_iso()
    except Exception as error:
        return _finish_failed(
            current,
            phase="product_validation",
            code="required_product_validation_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    if not validation.passed:
        failures = "; ".join(
            f"{check.path}: {check.reason}"
            for check in validation.checks
            if not check.passed
        )
        return _finish_failed(
            current,
            phase="product_validation",
            code="required_product_validation_failed",
            message=f"required product validation failed: {failures}",
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        _append_log(
            current,
            phase="product_validation",
            severity="INFO",
            message="required product validation passed",
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="product_validation",
            code="service_log_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            record_failure_log=False,
        )
    try:
        current = _set_phase(current, "record_finalization")
    except Exception as error:
        return _finish_failed(
            current,
            phase="record_finalization",
            code="phase_recording_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )
    try:
        _append_log(
            current,
            phase="record_finalization",
            severity="INFO",
            message="publishing terminal calculation evidence",
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="record_finalization",
            code="service_log_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            record_failure_log=False,
        )
    try:
        product_manifest.publish_product_manifest(current, validation)
    except Exception as error:
        return _finish_failed(
            current,
            phase="record_finalization",
            code="product_manifest_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
        )

    terminal_at = _now_iso()
    try:
        provenance.publish_provenance(
            current,
            _provenance_facts(
                current,
                configuration_materialization=configuration_facts,
                execution=execution,
                execution_started=execution_started,
                resolution=resolution,
                validated_at=validated_at,
                terminal_at=terminal_at,
                failure=None,
            ),
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="record_finalization",
            code="provenance_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            terminal_at=terminal_at,
        )
    try:
        terminal = calculation_finalization.finalize_success(
            current,
            execution=execution,
            validation=validation,
            terminal_timestamp=terminal_at,
        )
    except Exception as error:
        return _finish_failed(
            current,
            phase="record_finalization",
            code="success_finalization_failed",
            message=_error_text(error),
            configuration_materialization=configuration_facts,
            execution=execution,
            execution_started=execution_started,
            resolution=resolution,
            validation=validation,
            validated_at=validated_at,
            terminal_at=terminal_at,
        )
    recalculation.finalize_transaction(current.event_id)
    return terminal.status
