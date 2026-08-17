from __future__ import annotations

import copy
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from shakemap_service import (
    calculation,
    paths,
    product_validation,
    recalculation,
    required_products,
    status,
)
from shakemap_service.config import Settings
from shakemap_service.runner import ExecutionResult
from shakemap_service.submission import Upload, accept_request


class CalculationExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = status.new_queued_record(
            event_id="event-1",
            internal_sequence=7,
            selected_configuration="regional-a",
            overwrite=True,
            warnings=[],
            input_mode="xml",
        )
        self.record.status = status.LifecycleState.RUNNING.value
        self.record.timestamps["started_at"] = "2026-08-16T10:00:00.000000Z"
        self.execution = ExecutionResult(
            command=[
                "shake",
                "event-1",
                "select",
                "assemble",
                "model",
                "contour",
                "mapping",
                "stations",
                "gridxml",
            ],
            exit_code=0,
            signal=None,
            pid=4321,
            started_at="2026-08-16T10:01:00.000000Z",
            completed_at="2026-08-16T10:02:00.000000Z",
        )
        self.resolution = required_products.RequiredProductResolution(
            paths=("shake_result.hdf", "intensity.jpg"),
            source="derived",
        )
        self.validation = product_validation.ProductValidationResult(
            required_paths=self.resolution.paths,
            source=self.resolution.source,
            checks=tuple(
                product_validation.RequiredProductCheck(
                    path=path,
                    size=10,
                    passed=True,
                    reason="generic checks passed",
                )
                for path in self.resolution.paths
            ),
            passed=True,
        )

    def _terminal(self, state: status.LifecycleState) -> status.CalculationRecord:
        terminal = copy.deepcopy(self.record)
        terminal.status = state.value
        return terminal

    def _run_with_components(
        self,
        *,
        execution: ExecutionResult | None = None,
        profile_error: BaseException | None = None,
        resolution_error: BaseException | None = None,
        validation: product_validation.ProductValidationResult | None = None,
        validation_error: BaseException | None = None,
        manifest_error: BaseException | None = None,
        provenance_error: BaseException | None = None,
        log_error_on_call: int | None = None,
        finalization_error: BaseException | None = None,
        partial_manifest_error: BaseException | None = None,
        failure_provenance_error: BaseException | None = None,
        base_environment: dict[str, str] | None = None,
        runner_error: BaseException | None = None,
    ) -> tuple[str, dict[str, mock.Mock], list[str]]:
        calls: list[str] = []
        components: dict[str, mock.Mock] = {}
        context = SimpleNamespace(environment={"HOME": "/private/home"})
        materialization = SimpleNamespace(
            selected_configuration="regional-a",
            profile_name="calculation",
            source_directory=Path("/regional-a"),
            profile_helper=SimpleNamespace(
                command=("sm_profile",), output="profile", return_code=0
            ),
            strec_helper=SimpleNamespace(
                command=("strec_cfg",), output="strec", return_code=0
            ),
        )
        selected_execution = self.execution if execution is None else execution
        selected_validation = self.validation if validation is None else validation

        def remember(name: str, result: object = None):
            def side_effect(*args: object, **kwargs: object) -> object:
                calls.append(name)
                return result

            return side_effect

        def started_runner(
            event_id: str,
            *,
            log_file: Path,
            env: dict[str, str],
            on_started: object,
        ) -> ExecutionResult:
            calls.append("runner")
            on_started(  # type: ignore[operator]
                selected_execution.pid,
                list(selected_execution.command),
                selected_execution.started_at,
            )
            return selected_execution

        log_calls = 0

        def append_log(*args: object, **kwargs: object) -> None:
            nonlocal log_calls
            log_calls += 1
            calls.append("log")
            if log_calls == log_error_on_call:
                raise OSError("service log unavailable")

        manifest_calls = 0

        def publish_manifest(*args: object, **kwargs: object) -> Path:
            nonlocal manifest_calls
            manifest_calls += 1
            calls.append("manifest")
            is_partial = kwargs.get("primary_reason") is not None
            if is_partial and partial_manifest_error is not None:
                raise partial_manifest_error
            if not is_partial and manifest_error is not None:
                raise manifest_error
            return Path("/manifest")

        provenance_calls = 0

        def publish_provenance(*args: object, **kwargs: object) -> Path:
            nonlocal provenance_calls
            provenance_calls += 1
            calls.append("provenance")
            facts = args[1]
            if facts.failure is None and provenance_error is not None:
                raise provenance_error
            if facts.failure is not None and failure_provenance_error is not None:
                raise failure_provenance_error
            return Path("/provenance")

        patches = {
            "prepare": mock.patch.object(
                calculation.recalculation,
                "prepare_calculation",
                side_effect=remember(
                    "prepare", SimpleNamespace(record=self.record)
                ),
            ),
            "read_current": mock.patch.object(
                calculation.status, "read_current_record", return_value=self.record
            ),
            "update_current": mock.patch.object(
                calculation.status,
                "update_current_record",
                side_effect=lambda *args, **kwargs: self.record,
            ),
            "log": mock.patch.object(
                calculation.calculation_log,
                "append_service_log",
                side_effect=append_log,
            ),
            "shake_log": mock.patch.object(
                calculation,
                "_precreate_shake_log",
                side_effect=remember("shake_log", Path("/private/shake.log")),
            ),
            "context": mock.patch.object(
                calculation.native_context,
                "prepare_native_context",
                side_effect=remember("context", context),
            ),
            "profile": mock.patch.object(
                calculation.native_profile,
                "materialize_native_profile",
                side_effect=(
                    profile_error
                    if profile_error is not None
                    else remember("profile", materialization)
                ),
            ),
            "runner": mock.patch.object(
                calculation.runner,
                "run_shake",
                side_effect=runner_error if runner_error is not None else started_runner,
            ),
            "products_dir": mock.patch.object(
                calculation.paths,
                "event_native_products_dir",
                return_value=Path("/products/event-1/current/products"),
            ),
            "resolve": mock.patch.object(
                calculation.required_products,
                "resolve_required_products",
                side_effect=(
                    resolution_error
                    if resolution_error is not None
                    else remember("resolve", self.resolution)
                ),
            ),
            "validate": mock.patch.object(
                calculation.product_validation,
                "validate_required_products",
                side_effect=(
                    validation_error
                    if validation_error is not None
                    else remember("validate", selected_validation)
                ),
            ),
            "manifest": mock.patch.object(
                calculation.product_manifest,
                "publish_product_manifest",
                side_effect=publish_manifest,
            ),
            "provenance": mock.patch.object(
                calculation.provenance,
                "publish_provenance",
                side_effect=publish_provenance,
            ),
            "success": mock.patch.object(
                calculation.calculation_finalization,
                "finalize_success",
                side_effect=(
                    finalization_error
                    if finalization_error is not None
                    else remember(
                        "success",
                        self._terminal(status.LifecycleState.SUCCESS),
                    )
                ),
            ),
            "failure": mock.patch.object(
                calculation.calculation_finalization,
                "finalize_failure",
                side_effect=remember(
                    "failure", self._terminal(status.LifecycleState.FAILED)
                ),
            ),
            "transaction": mock.patch.object(
                calculation.recalculation,
                "finalize_transaction",
                side_effect=remember("transaction", True),
            ),
        }
        supplied_environment = (
            {"KEEP": "unchanged"}
            if base_environment is None
            else base_environment
        )
        with contextlib.ExitStack() as stack:
            for name, patcher in patches.items():
                components[name] = stack.enter_context(patcher)
            result = calculation.execute_calculation(
                self.record,
                base_environment=supplied_environment,
            )
        return result, components, calls

    def test_success_composes_each_component_once_in_order(self) -> None:
        base_environment = {"KEEP": "unchanged", "HOME": "/caller"}
        original = dict(base_environment)
        result, components, calls = self._run_with_components(
            base_environment=base_environment
        )

        self.assertEqual(result, status.LifecycleState.SUCCESS.value)
        for name in (
            "prepare",
            "shake_log",
            "context",
            "profile",
            "runner",
            "resolve",
            "validate",
            "manifest",
            "provenance",
            "success",
            "transaction",
        ):
            self.assertEqual(components[name].call_count, 1, name)
        self.assertLess(calls.index("prepare"), calls.index("shake_log"))
        self.assertLess(calls.index("shake_log"), calls.index("context"))
        self.assertLess(calls.index("context"), calls.index("profile"))
        self.assertLess(calls.index("profile"), calls.index("runner"))
        self.assertLess(calls.index("runner"), calls.index("resolve"))
        self.assertLess(calls.index("resolve"), calls.index("validate"))
        self.assertLess(calls.index("validate"), calls.index("manifest"))
        self.assertLess(calls.index("manifest"), calls.index("provenance"))
        self.assertLess(calls.index("provenance"), calls.index("success"))
        self.assertLess(calls.index("success"), calls.index("transaction"))
        context_environment = components["context"].call_args.args[1]
        self.assertEqual(context_environment, original)
        self.assertIsNot(context_environment, base_environment)
        self.assertEqual(base_environment, original)
        profile_context = components["profile"].call_args.args[0]
        runner_call = components["runner"].call_args
        self.assertEqual(runner_call.args, ("event-1",))
        self.assertEqual(runner_call.kwargs["log_file"], Path("/private/shake.log"))
        self.assertIs(
            runner_call.kwargs["env"], profile_context.environment
        )
        components["validate"].assert_called_once_with(
            Path("/products/event-1/current/products"), self.resolution
        )
        components["manifest"].assert_called_once_with(
            self.record, self.validation
        )

        provenance_facts = components["provenance"].call_args.args[1]
        self.assertIs(provenance_facts.required_products, self.resolution)
        terminal_timestamp = components["success"].call_args.kwargs[
            "terminal_timestamp"
        ]
        self.assertEqual(
            provenance_facts.timestamps["terminal_at"], terminal_timestamp
        )
        components["transaction"].assert_called_once_with("event-1")

    def test_service_log_records_promoted_warnings_before_native_setup(self) -> None:
        self.record.warnings[:] = ["preceding service tree was missing"]
        _, components, calls = self._run_with_components()
        warning_calls = [
            call
            for call in components["log"].call_args_list
            if call.kwargs["severity"] == "WARNING"
        ]
        self.assertEqual(len(warning_calls), 1)
        self.assertEqual(
            warning_calls[0].kwargs["message"],
            "preceding service tree was missing",
        )
        self.assertLess(calls.index("log"), calls.index("shake_log"))

    def test_wrapper_phases_do_not_claim_native_module_transitions(self) -> None:
        _, components, _ = self._run_with_components()
        progress_updates = [
            call.kwargs["progress"]
            for call in components["update_current"].call_args_list
            if "progress" in call.kwargs
        ]
        self.assertEqual(
            [update["phase"] for update in progress_updates],
            [
                "calculation_preparation",
                "native_execution",
                "product_validation",
                "record_finalization",
            ],
        )
        self.assertTrue(
            all(update["current_module"] is None for update in progress_updates)
        )
        self.assertTrue(
            all(update["completed_modules"] == [] for update in progress_updates)
        )

    def test_profile_failure_is_primary_and_recording_failures_are_secondary(self) -> None:
        result, components, _ = self._run_with_components(
            profile_error=RuntimeError("profile rejected"),
            log_error_on_call=2,
            partial_manifest_error=OSError("manifest unavailable"),
            failure_provenance_error=OSError("provenance unavailable"),
        )
        self.assertEqual(result, status.LifecycleState.FAILED.value)
        failure_call = components["failure"].call_args
        self.assertEqual(
            failure_call.kwargs["code"], "configuration_materialization_failed"
        )
        self.assertEqual(failure_call.kwargs["message"], "profile rejected")
        self.assertEqual(
            [item["code"] for item in failure_call.kwargs["secondary_evidence"]],
            [
                "service_log_recording_failed",
                "partial_manifest_failed",
                "provenance_recording_failed",
            ],
        )
        components["runner"].assert_not_called()
        components["transaction"].assert_called_once_with("event-1")

    def test_native_nonzero_and_signal_retain_exact_execution_result(self) -> None:
        cases = (
            (4, None, "native_exit"),
            (None, 15, "native_signal"),
        )
        for exit_code, signal, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                execution = copy.deepcopy(self.execution)
                execution.exit_code = exit_code
                execution.signal = signal
                result, components, _ = self._run_with_components(
                    execution=execution
                )
                self.assertEqual(result, status.LifecycleState.FAILED.value)
                failure_call = components["failure"].call_args
                self.assertEqual(failure_call.kwargs["code"], expected_code)
                self.assertIs(failure_call.kwargs["execution"], execution)
                components["resolve"].assert_not_called()
                components["runner"].assert_called_once()

    def test_service_shutdown_is_distinct_from_native_failure(self) -> None:
        terminated = copy.deepcopy(self.execution)
        terminated.exit_code = None
        terminated.signal = 15
        terminated.service_terminated = True
        cases = (
            ({"execution": terminated}, terminated),
            ({"runner_error": calculation.runner.ServiceShutdownError("closed")}, None),
        )
        for arguments, expected_execution in cases:
            with self.subTest(arguments=arguments):
                result, components, _ = self._run_with_components(**arguments)
                self.assertEqual(result, status.LifecycleState.FAILED.value)
                failure = components["failure"].call_args.kwargs
                self.assertEqual(failure["code"], "service_shutdown")
                self.assertIn("service is shutting down", failure["message"])
                self.assertIs(failure["execution"], expected_execution)
                components["resolve"].assert_not_called()

    def test_resolution_and_validation_failures_do_not_retry(self) -> None:
        failed_validation = copy.deepcopy(self.validation)
        failed_validation = product_validation.ProductValidationResult(
            required_paths=failed_validation.required_paths,
            source=failed_validation.source,
            checks=(
                product_validation.RequiredProductCheck(
                    path="shake_result.hdf",
                    size=None,
                    passed=False,
                    reason="product is missing",
                ),
            ),
            passed=False,
        )
        cases = (
            ({"resolution_error": RuntimeError("cannot resolve")},
             "required_product_resolution_failed", "resolve", "validate"),
            ({"validation_error": RuntimeError("cannot validate")},
             "required_product_validation_failed", "validate", None),
            ({"validation": failed_validation},
             "required_product_validation_failed", "validate", None),
        )
        for arguments, code, once_name, absent_name in cases:
            with self.subTest(code=code, arguments=arguments):
                result, components, _ = self._run_with_components(**arguments)
                self.assertEqual(result, status.LifecycleState.FAILED.value)
                self.assertEqual(components[once_name].call_count, 1)
                if absent_name is not None:
                    components[absent_name].assert_not_called()
                self.assertEqual(components["failure"].call_args.kwargs["code"], code)

    def test_success_evidence_failures_convert_once_to_failure_evidence(self) -> None:
        cases = (
            (
                "manifest_error",
                OSError("manifest failed"),
                "product_manifest_failed",
                1,
            ),
            (
                "provenance_error",
                OSError("provenance failed"),
                "provenance_failed",
                2,
            ),
        )
        for argument, error, code, provenance_calls in cases:
            with self.subTest(code=code):
                result, components, _ = self._run_with_components(
                    **{argument: error}
                )
                self.assertEqual(result, status.LifecycleState.FAILED.value)
                self.assertEqual(components["failure"].call_args.kwargs["code"], code)
                self.assertEqual(components["manifest"].call_count, 2)
                self.assertEqual(components["provenance"].call_count, provenance_calls)
                partial_call = components["manifest"].call_args_list[-1]
                self.assertEqual(
                    partial_call.kwargs["primary_reason"], str(error)
                )
                failure_facts = components["provenance"].call_args_list[-1].args[1]
                self.assertEqual(failure_facts.failure["code"], code)
                for component in ("profile", "runner", "resolve", "validate"):
                    self.assertEqual(components[component].call_count, 1, component)
                components["transaction"].assert_called_once()

    def test_terminal_evidence_failures_are_secondary_to_each_primary(self) -> None:
        cases = (
            (
                {"manifest_error": OSError("manifest primary")},
                "product_manifest_failed",
                "manifest primary",
            ),
            (
                {"provenance_error": OSError("provenance primary")},
                "provenance_failed",
                "provenance primary",
            ),
            (
                {"finalization_error": RuntimeError("finalization primary")},
                "success_finalization_failed",
                "finalization primary",
            ),
        )
        for arguments, code, message in cases:
            with self.subTest(code=code):
                result, components, _ = self._run_with_components(
                    **arguments,
                    partial_manifest_error=OSError("partial unavailable"),
                    failure_provenance_error=OSError("failure provenance unavailable"),
                )
                self.assertEqual(result, status.LifecycleState.FAILED.value)
                failure_call = components["failure"].call_args
                self.assertEqual(failure_call.kwargs["code"], code)
                self.assertEqual(failure_call.kwargs["message"], message)
                self.assertEqual(
                    [
                        item["code"]
                        for item in failure_call.kwargs["secondary_evidence"]
                    ],
                    ["partial_manifest_failed", "provenance_recording_failed"],
                )
                self.assertEqual(components["manifest"].call_count, 2)
                expected_provenance_calls = (
                    1 if code == "product_manifest_failed" else 2
                )
                self.assertEqual(
                    components["provenance"].call_count,
                    expected_provenance_calls,
                )
                for component in ("profile", "runner", "resolve", "validate"):
                    self.assertEqual(components[component].call_count, 1, component)

    def test_service_log_failure_is_not_retried(self) -> None:
        result, components, _ = self._run_with_components(log_error_on_call=1)
        self.assertEqual(result, status.LifecycleState.FAILED.value)
        self.assertEqual(components["log"].call_count, 1)
        self.assertEqual(
            components["failure"].call_args.kwargs["code"], "service_log_failed"
        )

    def test_success_finalization_failure_becomes_failed_before_transaction_cleanup(self) -> None:
        result, components, calls = self._run_with_components(
            finalization_error=RuntimeError("success gate rejected")
        )
        self.assertEqual(result, status.LifecycleState.FAILED.value)
        self.assertEqual(
            components["failure"].call_args.kwargs["code"],
            "success_finalization_failed",
        )
        self.assertEqual(components["provenance"].call_count, 2)
        self.assertEqual(components["manifest"].call_count, 2)
        self.assertEqual(
            components["manifest"].call_args_list[1].kwargs["primary_reason"],
            "success gate rejected",
        )
        failure_facts = components["provenance"].call_args_list[1].args[1]
        failure_timestamp = components["failure"].call_args.kwargs[
            "terminal_timestamp"
        ]
        self.assertEqual(
            failure_facts.timestamps["terminal_at"], failure_timestamp
        )
        self.assertEqual(
            failure_facts.failure["code"], "success_finalization_failed"
        )
        self.assertLess(calls.index("failure"), calls.index("transaction"))

    def test_preparation_failure_escapes_without_terminal_or_retry(self) -> None:
        with (
            mock.patch.object(
                calculation.recalculation,
                "prepare_calculation",
                side_effect=RuntimeError("transaction interrupted"),
            ) as prepare,
            mock.patch.object(
                calculation.calculation_finalization, "finalize_failure"
            ) as failure,
            mock.patch.object(
                calculation.recalculation, "finalize_transaction"
            ) as transaction,
        ):
            with self.assertRaisesRegex(RuntimeError, "transaction interrupted"):
                calculation.execute_calculation(
                    self.record,
                    base_environment={"KEEP": "value"},
                )
        prepare.assert_called_once_with(7)
        failure.assert_not_called()
        transaction.assert_not_called()

    def test_orchestrator_has_no_fallback_or_native_content_reader(self) -> None:
        source = Path(calculation.__file__).read_text(encoding="utf-8")
        for forbidden in (
            "h5py",
            "PIL",
            "ShakeMapOutputContainer",
            "fallback",
            "rmtree",
            "unlink",
        ):
            self.assertNotIn(forbidden, source)


class ShutdownFailurePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="shutdown_failure_")
        self.previous_paths_settings = paths.settings
        self.previous_status_settings = status.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )
        paths.settings = configured
        status.settings = configured

    def tearDown(self) -> None:
        status.settings = self.previous_status_settings
        paths.settings = self.previous_paths_settings
        self.temporary.cleanup()

    def test_service_termination_writes_durable_failure_and_partial_evidence(self) -> None:
        accepted = accept_request(
            "shutdown-event",
            [Upload("event.xml", io.BytesIO(b"input"))],
        )
        status.transition_to_running(accepted.internal_sequence)
        recalculation.prepare_calculation(accepted.internal_sequence)
        current = status.read_current_record("shutdown-event")
        self.assertIsNotNone(current)
        assert current is not None
        paths.event_logs_dir(current.event_id).mkdir(parents=True, exist_ok=True)
        paths.event_log_file(current.event_id).write_text(
            "native process received SIGTERM\n",
            encoding="utf-8",
        )
        execution = ExecutionResult(
            command=["shake", "shutdown-event"],
            exit_code=None,
            signal=15,
            pid=4321,
            started_at="2026-08-17T12:00:00.000000Z",
            completed_at="2026-08-17T12:00:01.000000Z",
            service_terminated=True,
        )

        with mock.patch.object(
            calculation.provenance.build_identity,
            "service_identity",
            return_value={"fixture": True},
        ):
            result = calculation._finish_failed(
                current,
                phase="native_execution",
                code="service_shutdown",
                message="native execution stopped because the service is shutting down",
                configuration_materialization={
                    "materialized": True,
                    "selected_configuration": "global",
                },
                execution=execution,
                execution_started=None,
                resolution=None,
                validation=None,
                validated_at=None,
            )

        self.assertEqual(result, status.LifecycleState.FAILED.value)
        terminal = status.read_current_record(current.event_id)
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal.failure["code"], "service_shutdown")
        self.assertEqual(terminal.native_outcome["signal"], 15)
        manifest = json.loads(
            paths.event_manifest_file(current.event_id).read_text(encoding="utf-8")
        )
        provenance = json.loads(
            paths.event_provenance_file(current.event_id).read_text(encoding="utf-8")
        )
        self.assertTrue(manifest["partial"])
        self.assertEqual(provenance["failure"]["code"], "service_shutdown")
        self.assertIn(
            "service is shutting down",
            paths.event_service_log_file(current.event_id).read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
