from __future__ import annotations

import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import MappingProxyType
from unittest import mock

from shakemap_service import native_context, paths, status
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


class NativeContextTests(unittest.TestCase):
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

    def tearDown(self) -> None:
        paths.settings = self.previous_path_settings
        status.settings = self.previous_status_settings
        self.temporary.cleanup()

    def _accept(self, event_id: str):
        return accept_request(
            event_id,
            [Upload("event.xml", io.BytesIO(event_id.encode("utf-8")))],
        )

    def _promote(
        self,
        event_id: str,
        *,
        create_native_current: bool = True,
    ) -> status.CalculationRecord:
        accepted = self._accept(event_id)
        status.transition_to_running(accepted.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(accepted.internal_sequence).rename(
            paths.event_service_dir(event_id)
        )
        if create_native_current:
            paths.event_current_dir(event_id).mkdir(parents=True)
        current = status.read_current_record(event_id)
        if current is None:
            raise AssertionError("promoted test record is missing")
        return current

    def test_creates_exact_private_layout_and_environment(self) -> None:
        record = self._promote("evt")
        supplied = MappingProxyType(
            {
                "KEEP": "preserved",
                "HOME": "/shared/home",
                "CALLED_FROM_PYTEST": "1",
                "CALLED_FROM_MAIN": "1",
                "CARTOPY_DATA_DIR": "/shared/cartopy",
            }
        )
        supplied_before = dict(supplied)
        process_environment_before = dict(os.environ)

        with mock.patch("subprocess.Popen") as popen:
            context = native_context.prepare_native_context(record, supplied)

        profile = paths.event_profile_dir("evt")
        expected_children = {
            Path("home"),
            Path("cache"),
            Path("cache/xdg"),
            Path("cache/xdg-config"),
            Path("cache/matplotlib"),
            Path("cache/numba"),
            Path("tmp"),
        }
        self.assertEqual(
            {path.relative_to(profile) for path in profile.rglob("*")},
            expected_children,
        )
        for directory in (profile, *(profile / path for path in expected_children)):
            self.assertTrue(directory.is_dir())
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode) & 0o077, 0)

        self.assertEqual(context.event_id, "evt")
        self.assertEqual(context.internal_sequence, record.internal_sequence)
        self.assertEqual(context.profile_directory, profile)
        self.assertEqual(context.home_directory, profile / "home")
        self.assertEqual(context.install_directory, profile / "install")
        self.assertFalse(context.install_directory.exists())
        self.assertEqual(context.data_directory, paths.products_dir())
        self.assertEqual(context.environment["KEEP"], "preserved")
        self.assertNotIn("CALLED_FROM_PYTEST", context.environment)
        self.assertNotIn("CALLED_FROM_MAIN", context.environment)
        self.assertEqual(context.environment["HOME"], str(profile / "home"))
        self.assertEqual(
            context.environment["XDG_CACHE_HOME"],
            str(profile / "cache" / "xdg"),
        )
        self.assertEqual(
            context.environment["XDG_CONFIG_HOME"],
            str(profile / "cache" / "xdg-config"),
        )
        self.assertEqual(
            context.environment["MPLCONFIGDIR"],
            str(profile / "cache" / "matplotlib"),
        )
        self.assertEqual(
            context.environment["NUMBA_CACHE_DIR"],
            str(profile / "cache" / "numba"),
        )
        self.assertEqual(context.environment["TMPDIR"], str(profile / "tmp"))
        self.assertEqual(
            context.environment["CARTOPY_DATA_DIR"],
            "/opt/shakemap-support/cartopy",
        )
        self.assertEqual(dict(supplied), supplied_before)
        self.assertEqual(dict(os.environ), process_environment_before)
        popen.assert_not_called()

    def test_rejects_nonrunning_supplied_record_before_writing(self) -> None:
        accepted = self._accept("queued")
        record = status.read_status(accepted.internal_sequence)
        if record is None:
            raise AssertionError("queued test record is missing")

        with self.assertRaisesRegex(ValueError, "must be RUNNING"):
            native_context.prepare_native_context(record, {})

        self.assertFalse(paths.event_profile_dir("queued").exists())

    def test_requires_matching_running_current_record_before_writing(self) -> None:
        missing = self._accept("missing-current")
        missing_record = status.transition_to_running(missing.internal_sequence)
        paths.event_current_dir("missing-current").mkdir(parents=True)
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            native_context.prepare_native_context(missing_record, {})
        self.assertFalse(paths.event_profile_dir("missing-current").exists())

        self._promote("identity")
        later = self._accept("identity")
        later_record = status.transition_to_running(later.internal_sequence)
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            native_context.prepare_native_context(later_record, {})
        self.assertFalse(paths.event_profile_dir("identity").exists())

        running = self._promote("terminal-current")
        status.transition_current_record(
            "terminal-current",
            status.LifecycleState.FAILED,
            failure={"code": "test_failure", "message": "fixture"},
            service_outcome={"completed": True, "successful": False},
        )
        with self.assertRaisesRegex(ValueError, "current calculation record must"):
            native_context.prepare_native_context(running, {})
        self.assertFalse(paths.event_profile_dir("terminal-current").exists())

    def test_requires_native_current_directory_before_writing(self) -> None:
        missing = self._promote("missing-native", create_native_current=False)
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            native_context.prepare_native_context(missing, {})
        self.assertFalse(paths.event_profile_dir("missing-native").exists())

        not_directory = self._promote("file-native", create_native_current=False)
        paths.event_products_dir("file-native").mkdir(parents=True)
        paths.event_current_dir("file-native").write_bytes(b"not a directory")
        with self.assertRaisesRegex(NotADirectoryError, "not a directory"):
            native_context.prepare_native_context(not_directory, {})
        self.assertFalse(paths.event_profile_dir("file-native").exists())

    def test_refuses_to_merge_or_replace_existing_profile(self) -> None:
        record = self._promote("existing")
        profile = paths.event_profile_dir("existing")
        profile.mkdir()
        marker = profile / "operator-marker"
        marker.write_bytes(b"preserve")

        with self.assertRaises(FileExistsError):
            native_context.prepare_native_context(record, {})

        self.assertEqual(marker.read_bytes(), b"preserve")
        self.assertEqual(list(profile.iterdir()), [marker])

    def test_contexts_for_different_calculations_are_disjoint(self) -> None:
        first = native_context.prepare_native_context(self._promote("first"), {})
        second = native_context.prepare_native_context(self._promote("second"), {})

        self.assertNotEqual(first.profile_directory, second.profile_directory)
        self.assertNotEqual(first.home_directory, second.home_directory)
        self.assertNotEqual(first.environment["HOME"], second.environment["HOME"])
        self.assertFalse(first.profile_directory.is_relative_to(second.profile_directory))
        self.assertFalse(second.profile_directory.is_relative_to(first.profile_directory))

    def test_filesystem_failure_is_confined_to_attempted_profile(self) -> None:
        record = self._promote("failure")
        profile = paths.event_profile_dir("failure")
        blocked_directory = profile / "cache" / "numba"
        native_marker = paths.event_current_dir("failure") / "native-marker"
        native_marker.write_bytes(b"native")
        input_file = paths.event_input_dir("failure") / "event.xml"
        status_before = paths.event_status_file("failure").read_bytes()
        service_tree_before = set(paths.service_root().rglob("*"))
        original_mkdir = Path.mkdir

        def fail_one_directory(
            directory: Path,
            mode: int = 0o777,
            parents: bool = False,
            exist_ok: bool = False,
        ) -> None:
            if directory == blocked_directory:
                raise OSError("injected directory failure")
            original_mkdir(
                directory,
                mode=mode,
                parents=parents,
                exist_ok=exist_ok,
            )

        with mock.patch.object(Path, "mkdir", new=fail_one_directory):
            with self.assertRaisesRegex(OSError, "injected directory failure"):
                native_context.prepare_native_context(record, {})

        added_paths = set(paths.service_root().rglob("*")) - service_tree_before
        self.assertTrue(added_paths)
        self.assertTrue(
            all(path == profile or path.is_relative_to(profile) for path in added_paths)
        )
        self.assertEqual(input_file.read_bytes(), b"failure")
        self.assertEqual(native_marker.read_bytes(), b"native")
        self.assertEqual(paths.event_status_file("failure").read_bytes(), status_before)


if __name__ == "__main__":
    unittest.main()
