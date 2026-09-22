"""Operator-facing recovery must follow the failed operation and real errno."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from shakemap_service import finalization, paths, preparation
from shakemap_service import access_diagnostics
from shakemap_service.config import Settings


class PermissionDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="operator's runtime ")
        self.runtime = Path(self.temporary.name)
        self.data = self.runtime / 'shakemap/data'
        self.target = self.data / 'global/vs30/grid.grd'
        self.payload = b'\x89HDF\r\n\x1a\nfixture'
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(self.payload)
        self.spec = {
            'label': 'global Vs30 grid', 'relative': 'global/vs30/grid.grd',
            'size': len(self.payload), 'sha256': hashlib.sha256(self.payload).hexdigest(),
            'url': 'https://example.invalid/grid', 'checksum_authority': 'fixture',
        }
        self.assets = patch.object(preparation, 'GLOBAL_ASSETS', {'vs30': self.spec})
        self.assets.start()
        self.previous_settings = paths.settings
        paths.settings = Settings(runtime_root=str(self.runtime))

    def tearDown(self) -> None:
        paths.settings = self.previous_settings
        self.assets.stop()
        self.temporary.cleanup()

    def data_command(self, action: str, *options: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stderr(output), redirect_stdout(output):
            code = preparation.main([action, '--data-root', str(self.data), *options])
        return code, output.getvalue()

    def suggested_commands(self, message: str) -> list[list[str]]:
        """Parse complete printed command lines, including any stray suffixes."""
        return [shlex.split(line) for line in message.splitlines() if ".sh" in line]

    def assert_data_repair(self, message: str, action: str) -> None:
        expected_argv = [
            str(preparation.PROJECT_ROOT / 'scripts/fix-shakemap-permissions.sh'),
            '--runtime-root', str(self.runtime), '--target', 'global/vs30/grid.grd',
        ]
        expected = shlex.join(expected_argv)
        self.assertIn(expected, message)
        self.assertIn(f'sudo {expected}', message)
        self.assertIn(f'manage-shakemap-data.sh {action} --runtime', message)
        self.assertNotIn('move or remove', message.lower())
        self.assertNotIn('provision the missing', message)
        self.assertIn('access failure does not establish invalid content or absence', message)
        self.assertEqual(self.suggested_commands(message), [
            expected_argv,
            ["sudo", *expected_argv],
            [str(preparation.PROJECT_ROOT / "scripts/manage-shakemap-data.sh"),
             action, "--runtime", str(self.runtime)],
        ])

    def test_provision_retry_command_preserves_exact_source_and_download_arguments(self) -> None:
        source = self.runtime / "manual source's.grd"
        topography = self.runtime / "topography source's.grd"
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == self.target:
                raise PermissionError(errno.EACCES, "injected denial", str(path))
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", denied):
            code, message = self.data_command(
                "provision", "--vs30-source", str(source),
                "--topo-source", str(topography), "--no-download",
            )
        self.assertEqual(code, 2)
        self.assertEqual(self.suggested_commands(message)[-1], [
            str(preparation.PROJECT_ROOT / "scripts/manage-shakemap-data.sh"),
            "provision", "--runtime", str(self.runtime),
            "--vs30-source", str(source), "--topo-source", str(topography),
            "--no-download",
        ])

    def test_data_target_command_preserves_spaces_and_apostrophes(self) -> None:
        relative = "regional/operator's configuration/grid file.grd"
        message = access_diagnostics.data_read_recovery(
            self.data, self.data / relative, "Then rerun the original validation."
        )
        expected = [
            str(preparation.PROJECT_ROOT / "scripts/fix-shakemap-permissions.sh"),
            "--runtime-root", str(self.runtime), "--target", relative,
        ]
        self.assertEqual(self.suggested_commands(message), [expected, ["sudo", *expected]])

    def test_finalizer_commands_separate_original_options_reminder(self) -> None:
        failure = PermissionError(errno.EACCES, "denied", str(paths.workflow_lock_file()))
        with patch.object(finalization, "begin", side_effect=failure):
            code, message = self.finalization_command("begin")
        self.assertEqual(code, 2)
        repair = [
            str(preparation.PROJECT_ROOT / "scripts/repair-shakemap-writable-paths.sh"),
            "--runtime-root", str(self.runtime),
        ]
        self.assertEqual(self.suggested_commands(message), [
            repair, ["sudo", *repair],
            [str(preparation.PROJECT_ROOT / "scripts/finalize-shakemap.sh"),
             "--runtime-root", str(self.runtime)],
        ])
        self.assertIn(
            "Reapply the original port/concurrency options to this command.",
            message.splitlines(),
        )

    def test_denied_file_and_parent_are_retained_for_validate_and_provision(self) -> None:
        original_open, original_lstat = Path.open, Path.lstat
        for action in ('validate', 'provision'):
            for failed_path in (self.target, self.target.parent):
                with self.subTest(action=action, path=failed_path):
                    def denied_open(path, *args, **kwargs):
                        if path == self.target and failed_path == self.target:
                            raise PermissionError(errno.EACCES, 'injected read denial', str(path))
                        return original_open(path, *args, **kwargs)

                    def denied_lstat(path, *args, **kwargs):
                        if path == self.target and failed_path == self.target.parent:
                            raise PermissionError(errno.EPERM, 'injected traversal denial', str(failed_path))
                        return original_lstat(path, *args, **kwargs)

                    with (
                        patch.object(Path, 'open', denied_open),
                        patch.object(Path, 'lstat', denied_lstat),
                        patch.object(preparation, 'download') as download,
                        patch.object(preparation.os, 'link') as install,
                        patch.object(preparation.os, 'replace') as replace,
                        patch.object(Path, 'unlink') as unlink,
                    ):
                        code, output = self.data_command(action)
                    message = (json.loads(output)['global_assets']['vs30']['corrective_action']
                               if action == 'validate' else output)
                    self.assertNotEqual(code, 0)
                    self.assert_data_repair(message, action)
                    self.assertIn(str(failed_path), output)
                    for operation in (download, install, replace, unlink):
                        operation.assert_not_called()
                    self.assertEqual(self.target.read_bytes(), self.payload)

    def test_storage_errors_do_not_suggest_permission_repair_or_reprovision(self) -> None:
        original = Path.open
        for error_number in (errno.ENOSPC, errno.EIO, errno.EROFS):
            for action in ('validate', 'provision'):
                with self.subTest(errno=error_number, action=action):
                    def broken(path, *args, **kwargs):
                        if path == self.target:
                            raise OSError(error_number, 'injected storage failure', str(path))
                        return original(path, *args, **kwargs)

                    with patch.object(Path, 'open', broken), patch.object(preparation, 'download') as download:
                        code, output = self.data_command(action)
                    self.assertNotEqual(code, 0)
                    for forbidden in ('sudo', 'fix-shakemap-permissions', 'move or remove', 'provision the missing'):
                        self.assertNotIn(forbidden, output)
                    self.assertIn('filesystem/storage', output)
                    download.assert_not_called()

    def test_checksum_mismatch_is_distinct_from_access_failure(self) -> None:
        self.target.write_bytes(self.payload[:-1] + b'X')
        code, output = self.data_command('validate')
        result = json.loads(output)['global_assets']['vs30']
        self.assertEqual(code, 1)
        self.assertEqual(result['reason'], 'checksum mismatch')
        self.assertIn('move or remove', result['corrective_action'])
        self.assertNotIn('sudo', output)

    def test_import_write_denial_identifies_destination_without_read_repair(self) -> None:
        source = self.runtime / "manual source's.grd"
        source.write_bytes(self.payload)
        self.target.unlink()

        def denied_copy(_source, destination):
            raise PermissionError(errno.EACCES, 'cannot create destination', str(destination))

        with patch.object(preparation.shutil, 'copyfile', side_effect=denied_copy):
            code, output = self.data_command('provision', '--vs30-source', str(source), '--no-download')
        self.assertEqual(code, 2)
        self.assertIn('target-parent write/traverse', output)
        self.assertIn('.grid.grd.install-', output)
        self.assertIn(shlex.quote(str(source)), output)
        self.assertIn('--no-download', output)
        self.assertNotIn('fix-shakemap-permissions.sh', output)
        self.assertNotIn('sudo', output)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.suggested_commands(output), [[
            str(preparation.PROJECT_ROOT / "scripts/manage-shakemap-data.sh"),
            "provision", "--runtime", str(self.runtime),
            "--vs30-source", str(source), "--no-download",
        ]])

    def test_uncontracted_private_path_does_not_offer_host_data_helper(self) -> None:
        recovery = access_diagnostics.data_read_recovery(
            Path('/opt/private/data'), Path('/opt/private/data/global/grid'), 'retry validation')
        self.assertNotIn('fix-shakemap-permissions', recovery)
        self.assertNotIn('sudo', recovery)

    def test_custom_data_root_retry_keeps_the_original_command(self) -> None:
        original_data = self.data
        self.data = self.runtime / 'custom-assets'
        target = self.data / self.spec['relative']
        original = Path.lstat

        def denied(path, *args, **kwargs):
            if path == target:
                raise PermissionError(errno.EACCES, 'denied', str(path))
            return original(path, *args, **kwargs)

        try:
            with patch.object(Path, 'lstat', denied):
                code, output = self.data_command('validate')
        finally:
            self.data = original_data
        self.assertEqual(code, 1)
        self.assertIn('rerun the original host data command', output)
        self.assertNotIn('manage-shakemap-data.sh', output)
        self.assertNotIn('fix-shakemap-permissions.sh', output)

    def test_container_recovery_uses_shared_host_runtime_and_host_helper(self) -> None:
        with (
            patch.dict(os.environ, {'SHAKEMAP_SHARED_RUNTIME_ROOT': str(self.runtime)}),
            patch.object(access_diagnostics, 'PROJECT_ROOT', Path('/app')),
        ):
            recovery = access_diagnostics.data_read_recovery(
                Path('/home/sysop/runtime/shakemap/data'),
                Path('/home/sysop/runtime/shakemap/data/global/vs30/grid.grd'),
                access_diagnostics.finalization_retry(Path('/home/sysop/runtime')),
            )
        self.assertIn('./scripts/fix-shakemap-permissions.sh', recovery)
        self.assertIn(shlex.quote(str(self.runtime)), recovery)
        self.assertIn('./scripts/finalize-shakemap.sh', recovery)
        self.assertNotIn('/app/scripts', recovery)
        self.assertNotIn('/home/sysop/runtime', recovery)
        repair = [
            "./scripts/fix-shakemap-permissions.sh", "--runtime-root", str(self.runtime),
            "--target", "global/vs30/grid.grd",
        ]
        self.assertEqual(self.suggested_commands(recovery), [
            repair, ["sudo", *repair],
            ["./scripts/finalize-shakemap.sh", "--runtime-root", str(self.runtime)],
        ])

    def finalization_command(self, command: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stderr(output), redirect_stdout(output):
            code = finalization.main([command])
        return code, output.getvalue()

    def test_wrapped_directory_denial_preserves_absolute_attempted_path(self) -> None:
        original = os.open

        def denied(path, *args, **kwargs):
            if path == 'products':
                raise PermissionError(errno.EACCES, 'denied', path)
            return original(path, *args, **kwargs)

        with patch.object(os, 'open', denied):
            code, output = self.finalization_command('prepare-runtime')
        self.assertEqual(code, 2)
        self.assertIn(str(self.runtime / 'shakemap/products'), output)
        self.assertIn('repair-shakemap-writable-paths.sh', output)
        self.assertIn('Then rerun', output)
        self.assertNotIn('workflow.lock', output)

    def test_begin_lock_denial_preserves_descriptor_relative_filename(self) -> None:
        original = os.open

        def denied(path, *args, **kwargs):
            if path == 'workflow.lock':
                raise PermissionError(errno.EACCES, 'denied', path)
            return original(path, *args, **kwargs)

        with patch.object(os, 'open', denied):
            code, output = self.finalization_command('begin')
        self.assertEqual(code, 2)
        self.assertIn(str(paths.workflow_lock_file()), output)
        self.assertIn('repair-shakemap-writable-paths.sh', output)

    def test_scientific_creation_and_ancestor_denials_do_not_offer_wrong_helper(self) -> None:
        for component in ('shakemap', 'global'):
            original = os.open

            def denied(path, *args, **kwargs):
                if path == component:
                    raise PermissionError(errno.EACCES, 'denied', path)
                return original(path, *args, **kwargs)

            with self.subTest(component=component), patch.object(os, 'open', denied):
                code, output = self.finalization_command('prepare-runtime')
            self.assertEqual(code, 2)
            self.assertIn('outside the service-writable repair scope', output)
            self.assertNotIn('sudo', output)
            self.assertNotIn('fix-shakemap-permissions.sh', output)

    def test_non_permission_refusals_keep_the_original_diagnostic(self) -> None:
        errors = [
            finalization.FinalizationError('accepted calculations are unfinished; wait for them to finish'),
            finalization.FinalizationError('durable calculation state is malformed: event'),
            ValueError('unsafe service directory ancestry'),
            FileNotFoundError(errno.ENOENT, 'missing', str(paths.service_dir())),
            *[OSError(number, 'storage failure', str(paths.service_dir()))
              for number in (errno.ENOSPC, errno.EIO, errno.EROFS)],
        ]
        for error in errors:
            with self.subTest(error=error), patch.object(finalization, 'begin', side_effect=error):
                code, output = self.finalization_command('begin')
            self.assertEqual(code, 2)
            self.assertIn(str(error), output)
            self.assertNotIn('sudo', output)
            self.assertNotIn('repair-shakemap', output)


if __name__ == '__main__':
    unittest.main()
