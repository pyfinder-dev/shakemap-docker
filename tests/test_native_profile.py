from __future__ import annotations

import dataclasses
import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import native_context, native_profile, paths, status
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


class NativeProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_path_settings = paths.settings
        self.previous_status_settings = status.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )
        paths.settings = configured
        status.settings = configured
        self.package_root = Path(self.temporary.name) / "installed-package"
        mapping = self.package_root / "data" / "mapping"
        (mapping / "nested").mkdir(parents=True)
        (mapping / "map.dat").write_bytes(b"mapping-data")
        (mapping / "nested" / "layer.dat").write_bytes(b"nested-data")
        self.model_base = (
            b"# model\r\n  vs30file = old-vs30\r\nmodel_tail = \xff\r\n"
        )
        self.products_base = (
            b"# products\n\ttopography = old-topography\nproducts_tail = yes\n"
        )
        self.strec_base = (
            b"[DATA]\n"
            b"folder = /opt/shakemap-support/strec\n"
            b"slabfolder = /opt/shakemap-support/strec/slabs\n"
            b"dbfile = /opt/shakemap-support/strec/moment_tensors.db\n"
            b"constant = preserved\n"
        )
        self.profile_output = "profile combined output\n"
        self.strec_output = "strec combined output\n"

    def tearDown(self) -> None:
        paths.settings = self.previous_path_settings
        status.settings = self.previous_status_settings
        self.temporary.cleanup()

    def _context(
        self,
        event_id: str,
        *,
        configuration: str = "global",
    ) -> native_context.NativeCalculationContext:
        accepted = accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(event_id.encode("utf-8")))],
            configuration=configuration,
        )
        status.transition_to_running(accepted.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(accepted.internal_sequence).rename(
            paths.event_service_dir(event_id)
        )
        paths.event_current_dir(event_id).mkdir(parents=True)
        record = status.read_current_record(event_id)
        if record is None:
            raise AssertionError("promoted test record is missing")
        return native_context.prepare_native_context(
            record,
            {"PRESERVED": event_id},
        )

    def _write_native_base(
        self,
        context: native_context.NativeCalculationContext,
        *,
        missing_base: str | None = None,
        mapping_entry: bool = False,
    ) -> None:
        configuration = context.install_directory / "config"
        mapping = context.install_directory / "data" / "mapping"
        configuration.mkdir(parents=True)
        mapping.mkdir(parents=True)
        selector = context.home_directory / ".shakemap" / "profiles.conf"
        selector.parent.mkdir()
        selector.write_text(
            "\ufeffprofile = calculation\n"
            "[profiles]\n"
            "[[calculation]]\n"
            f"install_path = {context.install_directory}\n"
            f"data_path = {context.data_directory}\n",
            encoding="utf-8",
        )
        for filename in native_profile.BASE_CONFIGURATION_FILES:
            if filename == missing_base:
                continue
            content = f"base-{filename}\n".encode("ascii")
            if filename == "model.conf":
                content = self.model_base
            elif filename == "products.conf":
                content = self.products_base
            (configuration / filename).write_bytes(content)
        if mapping_entry:
            (mapping / "unexpected").write_bytes(b"unexpected")

    def _write_strec(self, context: native_context.NativeCalculationContext) -> None:
        target = context.home_directory / ".strec" / "config.ini"
        target.parent.mkdir()
        target.write_bytes(self.strec_base)

    def _successful_helpers(
        self,
        context: native_context.NativeCalculationContext,
    ):
        def run(command, **kwargs):
            selected = tuple(command)
            if selected[0] == "sm_profile":
                self._write_native_base(context)
                return subprocess.CompletedProcess(
                    selected,
                    0,
                    stdout=self.profile_output,
                )
            if selected[0] == "strec_cfg":
                self._write_strec(context)
                return subprocess.CompletedProcess(
                    selected,
                    0,
                    stdout=self.strec_output,
                )
            raise AssertionError(f"unexpected helper command: {selected}")

        return run

    def _materialize(
        self,
        context: native_context.NativeCalculationContext,
        side_effect=None,
    ):
        helper = self._successful_helpers(context) if side_effect is None else side_effect
        with mock.patch.object(
            native_profile.resources,
            "files",
            return_value=self.package_root,
        ):
            with mock.patch.object(
                native_profile.subprocess,
                "run",
                side_effect=helper,
            ) as runner:
                result = native_profile.materialize_native_profile(context)
        return result, runner

    def _regional_sources(self, selected: str) -> dict[str, bytes]:
        source = paths.regional_data_dir() / selected
        source.mkdir(parents=True)
        contents: dict[str, bytes] = {}
        for index, filename in enumerate(native_profile.REGIONAL_CONFIGURATION_FILES):
            payload = f"{filename}:{index}\n".encode("ascii") + bytes([0x80 + index])
            (source / filename).write_bytes(payload)
            contents[filename] = payload
        (source / "ignored.conf").write_bytes(b"ignored")
        return contents

    def test_global_materialization_uses_exact_helpers_and_targeted_paths(self) -> None:
        context = self._context("global-event")
        environment_before = dict(context.environment)

        result, runner = self._materialize(context)

        expected_profile_call = mock.call(
            ("sm_profile", "-c", "calculation", "-n"),
            input=f"{context.install_directory}\n{context.data_directory}\n",
            env=context.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            shell=False,
        )
        expected_strec_call = mock.call(
            (
                "strec_cfg",
                "update",
                "--datafolder",
                "/opt/shakemap-support/strec",
            ),
            env=context.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            shell=False,
        )
        self.assertEqual(runner.call_args_list, [expected_profile_call, expected_strec_call])
        self.assertEqual(context.environment, environment_before)

        configuration = context.install_directory / "config"
        expected_model = self.model_base.replace(
            b"old-vs30",
            str(paths.vs30_grid_path()).encode("utf-8"),
        )
        expected_products = self.products_base.replace(
            b"old-topography",
            str(paths.topo_grid_path()).encode("utf-8"),
        )
        self.assertEqual((configuration / "model.conf").read_bytes(), expected_model)
        self.assertEqual(
            (configuration / "products.conf").read_bytes(),
            expected_products,
        )
        self.assertFalse(paths.vs30_grid_path().exists())
        self.assertFalse(paths.topo_grid_path().exists())
        self.assertEqual(
            (context.install_directory / "data/mapping/map.dat").read_bytes(),
            b"mapping-data",
        )
        self.assertEqual(
            (context.install_directory / "data/mapping/nested/layer.dat").read_bytes(),
            b"nested-data",
        )
        expected_strec = self.strec_base.replace(
            b"slabfolder = /opt/shakemap-support/strec/slabs",
            b"slabfolder = /opt/shakemap-support/slab2/slabs",
        )
        self.assertEqual(result.strec_configuration_file.read_bytes(), expected_strec)
        self.assertEqual(result.selected_configuration, "global")
        self.assertEqual(result.profile_name, "calculation")
        self.assertIsNone(result.source_directory)
        self.assertEqual(result.profile_directory, context.profile_directory)
        self.assertEqual(result.selector_file, context.home_directory / ".shakemap/profiles.conf")
        self.assertEqual(result.profile_helper.output, self.profile_output)
        self.assertEqual(result.profile_helper.return_code, 0)
        self.assertEqual(result.strec_helper.output, self.strec_output)
        self.assertEqual(result.strec_helper.return_code, 0)

    def test_regional_overlay_is_exact_and_does_not_apply_global_edits(self) -> None:
        contents = self._regional_sources("romania")
        source_directory = paths.regional_data_dir() / "romania"
        source_before = {
            path.name: (path.read_bytes(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in source_directory.iterdir()
        }
        context = self._context("regional-event", configuration="romania")

        result, runner = self._materialize(context)

        self.assertEqual(runner.call_count, 2)
        destination = context.install_directory / "config"
        for filename, payload in contents.items():
            self.assertEqual((destination / filename).read_bytes(), payload)
        self.assertFalse((destination / "ignored.conf").exists())
        self.assertEqual(result.source_directory, source_directory)
        self.assertNotIn(str(paths.vs30_grid_path()), (destination / "model.conf").read_text("latin-1"))
        self.assertNotIn(str(paths.topo_grid_path()), (destination / "products.conf").read_text("latin-1"))
        source_after = {
            path.name: (path.read_bytes(), hashlib.sha256(path.read_bytes()).hexdigest())
            for path in source_directory.iterdir()
        }
        self.assertEqual(source_after, source_before)

    def test_regional_sources_are_required_and_readable_before_helpers(self) -> None:
        source = paths.regional_data_dir() / "missing"
        source.mkdir(parents=True)
        for filename in native_profile.REGIONAL_CONFIGURATION_FILES[:-1]:
            (source / filename).write_bytes(b"source")
        context = self._context("missing-source", configuration="missing")

        with mock.patch.object(native_profile.subprocess, "run") as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(context)

        self.assertEqual(raised.exception.selected_configuration, "missing")
        self.assertEqual(raised.exception.stage, "regional_sources")
        runner.assert_not_called()
        self.assertFalse(context.install_directory.exists())
        self.assertFalse((context.home_directory / ".shakemap").exists())

        unreadable_context = self._context("unreadable-source", configuration="blocked")
        self._regional_sources("blocked")
        blocked = paths.regional_data_dir() / "blocked" / "model.conf"
        real_access = native_profile.os.access

        def access(path, mode):
            if Path(path) == blocked:
                return False
            return real_access(path, mode)

        with mock.patch.object(native_profile.os, "access", side_effect=access):
            with mock.patch.object(native_profile.subprocess, "run") as runner:
                with self.assertRaises(native_profile.NativeProfileError) as raised:
                    native_profile.materialize_native_profile(unreadable_context)
        self.assertEqual(raised.exception.stage, "regional_sources")
        runner.assert_not_called()

    def test_current_record_and_context_identity_are_checked_before_helpers(self) -> None:
        context = self._context("identity")
        mismatched = dataclasses.replace(
            context,
            internal_sequence=context.internal_sequence + 1,
        )
        with mock.patch.object(native_profile.subprocess, "run") as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(mismatched)
        self.assertEqual(raised.exception.stage, "current_record")
        runner.assert_not_called()

        wrong_path = dataclasses.replace(
            context,
            profile_directory=context.profile_directory / "other",
        )
        with mock.patch.object(native_profile.subprocess, "run") as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(wrong_path)
        self.assertEqual(raised.exception.stage, "current_record")
        runner.assert_not_called()

        status.transition_current_record(
            "identity",
            status.LifecycleState.FAILED,
            failure={"code": "test", "message": "test"},
            service_outcome={"completed": True, "successful": False},
        )
        with mock.patch.object(native_profile.subprocess, "run") as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(context)
        self.assertEqual(raised.exception.stage, "current_record")
        runner.assert_not_called()

    def test_existing_native_profile_outputs_are_never_merged_or_replaced(self) -> None:
        context = self._context("existing-output")
        context.install_directory.mkdir()
        marker = context.install_directory / "marker"
        marker.write_bytes(b"preserve")

        with mock.patch.object(native_profile.subprocess, "run") as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(context)

        self.assertEqual(raised.exception.stage, "profile_precondition")
        runner.assert_not_called()
        self.assertEqual(marker.read_bytes(), b"preserve")

    def test_profile_helper_spawn_and_nonzero_failures_are_single_attempts(self) -> None:
        spawn_context = self._context("spawn-failure")
        with mock.patch.object(
            native_profile.subprocess,
            "run",
            side_effect=FileNotFoundError("sm_profile missing"),
        ) as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(spawn_context)
        self.assertEqual(raised.exception.selected_configuration, "global")
        self.assertEqual(raised.exception.stage, "sm_profile_spawn")
        self.assertEqual(
            raised.exception.command,
            ("sm_profile", "-c", "calculation", "-n"),
        )
        self.assertIsNone(raised.exception.helper)
        self.assertEqual(runner.call_count, 1)

        nonzero_context = self._context("nonzero-failure")
        failure = subprocess.CompletedProcess(
            ("sm_profile", "-c", "calculation", "-n"),
            3,
            stdout="combined failure\n",
        )
        with mock.patch.object(
            native_profile.subprocess,
            "run",
            return_value=failure,
        ) as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(nonzero_context)
        self.assertEqual(raised.exception.stage, "sm_profile")
        self.assertEqual(raised.exception.helper.return_code, 3)
        self.assertEqual(raised.exception.helper.output, "combined failure\n")
        self.assertEqual(runner.call_count, 1)

    def test_profile_structure_and_mapping_source_fail_before_strec(self) -> None:
        missing_context = self._context("missing-base")

        def missing_base(command, **kwargs):
            self._write_native_base(missing_context, missing_base="shake.conf")
            return subprocess.CompletedProcess(command, 0, stdout="profile\n")

        with mock.patch.object(native_profile.subprocess, "run", side_effect=missing_base) as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(missing_context)
        self.assertEqual(raised.exception.stage, "profile_structure")
        self.assertEqual(runner.call_count, 1)

        mapping_context = self._context("missing-mapping")

        def profile_only(command, **kwargs):
            self._write_native_base(mapping_context)
            return subprocess.CompletedProcess(command, 0, stdout="profile\n")

        absent_package = Path(self.temporary.name) / "absent-package"
        with mock.patch.object(native_profile.resources, "files", return_value=absent_package):
            with mock.patch.object(native_profile.subprocess, "run", side_effect=profile_only) as runner:
                with self.assertRaises(native_profile.NativeProfileError) as raised:
                    native_profile.materialize_native_profile(mapping_context)
        self.assertEqual(raised.exception.stage, "mapping_copy")
        self.assertEqual(runner.call_count, 1)

    def test_unexpected_mapping_content_and_ambiguous_global_edit_fail_closed(self) -> None:
        mapping_context = self._context("mapping-content")

        def nonempty_mapping(command, **kwargs):
            self._write_native_base(mapping_context, mapping_entry=True)
            return subprocess.CompletedProcess(command, 0, stdout="profile\n")

        with mock.patch.object(native_profile.subprocess, "run", side_effect=nonempty_mapping) as runner:
            with self.assertRaises(native_profile.NativeProfileError) as raised:
                native_profile.materialize_native_profile(mapping_context)
        self.assertEqual(raised.exception.stage, "profile_structure")
        self.assertEqual(runner.call_count, 1)

        ambiguous_context = self._context("ambiguous-global")

        def ambiguous_profile(command, **kwargs):
            self._write_native_base(ambiguous_context)
            model = ambiguous_context.install_directory / "config/model.conf"
            model.write_bytes(self.model_base + b"vs30file = duplicate\n")
            return subprocess.CompletedProcess(command, 0, stdout="profile\n")

        with mock.patch.object(native_profile.resources, "files", return_value=self.package_root):
            with mock.patch.object(native_profile.subprocess, "run", side_effect=ambiguous_profile) as runner:
                with self.assertRaises(native_profile.NativeProfileError) as raised:
                    native_profile.materialize_native_profile(ambiguous_context)
        self.assertEqual(raised.exception.stage, "global_configuration")
        self.assertEqual(runner.call_count, 1)
        self.assertIn("found 2", str(raised.exception))

    def test_strec_failure_is_confined_and_ambiguous_assignment_is_rejected(self) -> None:
        context = self._context("strec-failure")
        native_marker = paths.event_current_dir("strec-failure") / "native-marker"
        native_marker.write_bytes(b"native")
        input_file = paths.event_input_dir("strec-failure") / "event.xml"
        status_before = paths.event_status_file("strec-failure").read_bytes()
        other_context = self._context("other-context")
        other_marker = other_context.profile_directory / "marker"
        other_marker.write_bytes(b"other")

        def fail_strec(command, **kwargs):
            if tuple(command)[0] == "sm_profile":
                self._write_native_base(context)
                return subprocess.CompletedProcess(command, 0, stdout="profile\n")
            return subprocess.CompletedProcess(command, 4, stdout="strec failed\n")

        with mock.patch.object(native_profile.resources, "files", return_value=self.package_root):
            with mock.patch.object(native_profile.subprocess, "run", side_effect=fail_strec) as runner:
                with self.assertRaises(native_profile.NativeProfileError) as raised:
                    native_profile.materialize_native_profile(context)
        self.assertEqual(raised.exception.stage, "strec_cfg")
        self.assertEqual(raised.exception.helper.return_code, 4)
        self.assertEqual(runner.call_count, 2)
        self.assertTrue(context.install_directory.is_dir())
        self.assertEqual(input_file.read_bytes(), b"strec-failure")
        self.assertEqual(native_marker.read_bytes(), b"native")
        self.assertEqual(paths.event_status_file("strec-failure").read_bytes(), status_before)
        self.assertEqual(other_marker.read_bytes(), b"other")

        ambiguous = self._context("ambiguous-strec")

        def duplicate_strec(command, **kwargs):
            if tuple(command)[0] == "sm_profile":
                self._write_native_base(ambiguous)
                return subprocess.CompletedProcess(command, 0, stdout="profile\n")
            self._write_strec(ambiguous)
            target = ambiguous.home_directory / ".strec/config.ini"
            target.write_bytes(self.strec_base + b"slabfolder = duplicate\n")
            return subprocess.CompletedProcess(command, 0, stdout="strec\n")

        with mock.patch.object(native_profile.resources, "files", return_value=self.package_root):
            with mock.patch.object(native_profile.subprocess, "run", side_effect=duplicate_strec) as runner:
                with self.assertRaises(native_profile.NativeProfileError) as raised:
                    native_profile.materialize_native_profile(ambiguous)
        self.assertEqual(raised.exception.stage, "strec_configuration")
        self.assertEqual(runner.call_count, 2)
        self.assertIn("found 2", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
