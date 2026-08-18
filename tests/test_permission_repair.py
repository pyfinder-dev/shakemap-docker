#!/usr/bin/env python3
"""Host tests for scientific-data permission repair."""
from __future__ import annotations

import os
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
HELPER = PROJECT / "scripts/fix-shakemap-permissions.sh"
FIXED_NAMES = ("global", "regional", "test")


class PermissionRepairTests(unittest.TestCase):
    def _make_runtime(self, parent: Path, name: str = "runtime") -> Path:
        runtime = parent / name
        data = runtime / "shakemap/data"
        for fixed_name in FIXED_NAMES:
            (data / fixed_name).mkdir(parents=True)
        return runtime

    def _run(
        self,
        runtime: Path,
        *extra: str,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(HELPER), "--runtime-root", str(runtime), *extra],
            cwd=PROJECT,
            env=environment,
            capture_output=True,
            text=True,
        )

    def _run_make(
        self,
        runtime: Path,
        target: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["make", "fix-permissions", f"RUNTIME_ROOT={runtime}"]
        if target is not None:
            command.append(f"PERMISSION_TARGET={target}")
        return subprocess.run(command, cwd=PROJECT, capture_output=True, text=True)

    @staticmethod
    def _mode(path: Path, *, follow_symlinks: bool = True) -> int:
        return stat.S_IMODE(path.stat().st_mode if follow_symlinks else path.lstat().st_mode)

    def _modes(self, paths: tuple[Path, ...]) -> dict[Path, int]:
        return {path: self._mode(path, follow_symlinks=False) for path in paths}

    def test_repairs_fixed_trees_and_preserves_data_ownership_and_existing_bits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime = self._make_runtime(base)
            data = runtime / "shakemap/data"
            nested = data / "global/grid collection/deep"
            nested.mkdir(parents=True)
            plain = nested / "grid.bin"
            executable = data / "regional/tool.dat"
            plain.write_bytes(b"scientific bytes\x00")
            executable.write_text("configuration\n", encoding="utf-8")
            plain.chmod(0o600)
            executable.chmod(0o2710)
            executable_mode = self._mode(executable)
            for path in (runtime, runtime / "shakemap", data):
                path.chmod(0o700)
            for path in (data / "global", data / "regional", data / "test", nested.parent, nested):
                path.chmod(0o700)

            owners = {path: (path.stat().st_uid, path.stat().st_gid) for path in (plain, executable, nested)}
            result = self._run(runtime)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(plain.read_bytes(), b"scientific bytes\x00")
            self.assertEqual(executable.read_text(encoding="utf-8"), "configuration\n")
            self.assertEqual(self._mode(plain), 0o644)
            self.assertEqual(self._mode(executable), executable_mode | 0o444)
            self.assertEqual(self._mode(nested), 0o755)
            for path in (runtime, runtime / "shakemap", data):
                self.assertEqual(self._mode(path), 0o711)
            for path, owner in owners.items():
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), owner)

    def test_make_handles_runtime_path_with_spaces_and_second_run_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._make_runtime(Path(temporary), "runtime with spaces")
            target = runtime / "shakemap/data/test/release file.xml"
            target.write_text("test", encoding="utf-8")
            target.chmod(0o600)
            command = ["make", "fix-permissions", f"RUNTIME_ROOT={runtime}"]

            first = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True)
            first_modes = {
                path: self._mode(path, follow_symlinks=False)
                for path in runtime.rglob("*")
            }
            second = subprocess.run(command, cwd=PROJECT, capture_output=True, text=True)
            second_modes = {
                path: self._mode(path, follow_symlinks=False)
                for path in runtime.rglob("*")
            }

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first_modes, second_modes)
            self.assertEqual(self._mode(target), 0o644)

    def test_explicit_file_repairs_only_that_file_and_required_ancestors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime = self._make_runtime(base, "runtime with spaces")
            data = runtime / "shakemap/data"
            parent = data / "global/config collection"
            parent.mkdir()
            relative = "global/config collection/config;`touch INJECTION_MARKER`"
            target = data / relative
            sibling = parent / "sibling.conf"
            unrelated = data / "regional/unrelated.conf"
            target.write_bytes(b"selected\x00content")
            sibling.write_bytes(b"sibling")
            unrelated.write_bytes(b"unrelated")
            target.chmod(0o6710)
            sibling.chmod(0o600)
            unrelated.chmod(0o600)
            for path, mode in (
                (runtime, 0o700),
                (runtime / "shakemap", 0o700),
                (data, 0o700),
                (data / "global", 0o2700),
                (parent, 0o3700),
                (data / "regional", 0o700),
                (data / "test", 0o700),
            ):
                path.chmod(mode)
            target_mode = self._mode(target)
            global_mode = self._mode(data / "global")
            parent_mode = self._mode(parent)
            owners = {
                path: (path.stat().st_uid, path.stat().st_gid)
                for path in (target, parent, sibling, unrelated)
            }
            marker = PROJECT / "INJECTION_MARKER"
            self.assertFalse(marker.exists())

            result = self._run_make(runtime, relative)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--target", result.stdout)
            self.assertEqual(target.read_bytes(), b"selected\x00content")
            self.assertEqual(self._mode(target), target_mode | 0o444)
            self.assertEqual(self._mode(runtime), 0o711)
            self.assertEqual(self._mode(runtime / "shakemap"), 0o711)
            self.assertEqual(self._mode(data), 0o711)
            self.assertEqual(self._mode(data / "global"), global_mode | 0o011)
            self.assertEqual(self._mode(parent), parent_mode | 0o011)
            self.assertEqual(self._mode(sibling), 0o600)
            self.assertEqual(self._mode(unrelated), 0o600)
            self.assertEqual(self._mode(data / "regional"), 0o700)
            self.assertEqual(self._mode(data / "test"), 0o700)
            self.assertFalse(marker.exists())
            for path, owner in owners.items():
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), owner)

    def test_explicit_directory_repairs_its_tree_without_touching_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._make_runtime(Path(temporary))
            data = runtime / "shakemap/data"
            selected = data / "regional/alpine data"
            nested = selected / "nested"
            nested.mkdir(parents=True)
            selected_file = nested / "grid.bin"
            selected_file.write_bytes(b"grid")
            sibling = data / "regional/sibling/private.conf"
            sibling.parent.mkdir()
            sibling.write_bytes(b"private")
            other_tree = data / "global/private.bin"
            other_tree.write_bytes(b"other")
            for path, mode in (
                (runtime, 0o700),
                (runtime / "shakemap", 0o700),
                (data, 0o700),
                (data / "regional", 0o700),
                (selected, 0o3700),
                (nested, 0o1700),
                (selected_file, 0o4710),
                (sibling.parent, 0o700),
                (sibling, 0o600),
                (other_tree, 0o600),
            ):
                path.chmod(mode)
            original_file_mode = self._mode(selected_file)
            original_selected_mode = self._mode(selected)
            original_nested_mode = self._mode(nested)
            owners = {
                path: (path.stat().st_uid, path.stat().st_gid)
                for path in (selected, nested, selected_file)
            }

            first = self._run(runtime, "--target", "regional/alpine data")
            first_modes = self._modes((selected, nested, selected_file))
            second = self._run(runtime, "--target", "regional/alpine data")

            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(first_modes, self._modes((selected, nested, selected_file)))
            self.assertEqual(self._mode(selected), original_selected_mode | 0o055)
            self.assertEqual(self._mode(nested), original_nested_mode | 0o055)
            self.assertEqual(self._mode(selected_file), original_file_mode | 0o444)
            self.assertEqual(self._mode(data / "regional"), 0o711)
            self.assertEqual(self._mode(sibling.parent), 0o700)
            self.assertEqual(self._mode(sibling), 0o600)
            self.assertEqual(self._mode(other_tree), 0o600)
            for path, owner in owners.items():
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), owner)

    def test_invalid_explicit_targets_fail_before_mutation(self) -> None:
        cases = (
            (("--target", ""), "--target requires"),
            (("--target", "/global/file"), "must be relative"),
            (("--target", "global/../regional/file"), "unsafe path component"),
            (("--target", "global/./file"), "unsafe path component"),
            (("--target", "global//file"), "empty path component"),
            (("--target", "global/"), "empty path component"),
            (("--target", "inputs/file"), "must begin with global, regional, or test"),
            (("--target", "global/missing"), "missing or inaccessible"),
            (
                ("--target", "global/safe.bin", "--target", "regional/other"),
                "specified only once",
            ),
        )
        for arguments, diagnostic in cases:
            with self.subTest(arguments=arguments), tempfile.TemporaryDirectory() as temporary:
                runtime = self._make_runtime(Path(temporary))
                data = runtime / "shakemap/data"
                safe = data / "global/safe.bin"
                safe.write_bytes(b"safe")
                safe.chmod(0o600)
                paths = (
                    runtime,
                    runtime / "shakemap",
                    data,
                    data / "global",
                    data / "regional",
                    data / "test",
                    safe,
                )
                for path in paths[:-1]:
                    path.chmod(0o700)
                original_modes = self._modes(paths)

                result = self._run(runtime, *arguments)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(diagnostic, result.stderr)
                self.assertEqual(self._modes(paths), original_modes)
                self.assertEqual(safe.read_bytes(), b"safe")

    def test_explicit_symlink_ancestry_and_leaf_fail_before_mutation(self) -> None:
        for link_position in ("ancestor", "leaf"):
            with self.subTest(link_position=link_position), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                runtime = self._make_runtime(base)
                data = runtime / "shakemap/data"
                outside = base / "outside"
                outside.mkdir()
                outside_file = outside / "private.bin"
                outside_file.write_bytes(b"outside")
                outside_file.chmod(0o600)
                safe = data / "global/safe.bin"
                safe.write_bytes(b"safe")
                safe.chmod(0o600)
                if link_position == "ancestor":
                    link = data / "regional/linked"
                    link.symlink_to(outside, target_is_directory=True)
                    target = "regional/linked/private.bin"
                else:
                    link = data / "regional/linked.bin"
                    link.symlink_to(outside_file)
                    target = "regional/linked.bin"
                paths = (runtime, runtime / "shakemap", data, data / "regional", safe)
                for path in paths[:-1]:
                    path.chmod(0o700)
                original_modes = self._modes(paths)

                result = self._run(runtime, "--target", target)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Unsafe symbolic link in target ancestry", result.stderr)
                self.assertIn(str(link), result.stderr)
                self.assertEqual(self._modes(paths), original_modes)
                self.assertEqual(self._mode(outside_file), 0o600)
                self.assertEqual(safe.read_bytes(), b"safe")

    def test_explicit_fifo_and_socket_targets_fail_before_mutation(self) -> None:
        for special_kind in ("FIFO", "socket"):
            with self.subTest(special_kind=special_kind), tempfile.TemporaryDirectory(dir="/tmp") as temporary:
                runtime = self._make_runtime(Path(temporary))
                data = runtime / "shakemap/data"
                special = data / f"test/unexpected-{special_kind.lower()}"
                safe = data / "test/safe.bin"
                safe.write_bytes(b"safe")
                safe.chmod(0o600)
                open_socket = None
                if special_kind == "FIFO":
                    os.mkfifo(special, 0o600)
                else:
                    open_socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        open_socket.bind(str(special))
                    except PermissionError:
                        open_socket.close()
                        source = HELPER.read_text(encoding="utf-8")
                        self.assertIn('elif [[ -S "$path" ]]', source)
                        continue
                paths = (runtime, runtime / "shakemap", data, data / "test", safe)
                for path in paths[:-1]:
                    path.chmod(0o700)
                original_modes = self._modes(paths)
                try:
                    result = self._run(runtime, "--target", f"test/{special.name}")
                finally:
                    if open_socket is not None:
                        open_socket.close()

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"Selected target is a {special_kind}", result.stderr)
                self.assertEqual(self._modes(paths), original_modes)
                self.assertEqual(self._mode(safe), 0o600)

        source = HELPER.read_text(encoding="utf-8")
        self.assertIn('elif [[ -b "$path" ]]', source)
        self.assertIn('elif [[ -c "$path" ]]', source)

    def test_uninspectable_explicit_directory_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._make_runtime(Path(temporary))
            data = runtime / "shakemap/data"
            selected = data / "global/locked"
            selected.mkdir()
            child = selected / "private.bin"
            child.write_bytes(b"private")
            child.chmod(0o600)
            selected.chmod(0o300)
            paths = (runtime, runtime / "shakemap", data, data / "global", selected, child)
            for path in paths[:4]:
                path.chmod(0o700)
            original_modes = self._modes(paths)
            try:
                result = self._run(runtime, "--target", "global/locked")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Could not inspect directory", result.stderr)
                self.assertEqual(self._modes(paths), original_modes)
            finally:
                selected.chmod(0o700)

    def test_usage_documents_default_file_and_directory_examples(self) -> None:
        result = subprocess.run(
            ["bash", str(HELPER), "--help"],
            cwd=PROJECT,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--target DATA_RELATIVE_PATH", result.stdout)
        self.assertIn("--runtime-root ./runtime\n", result.stdout)
        self.assertIn("--target 'regional/alps/config file.conf'", result.stdout)
        self.assertIn("--target global/vs30", result.stdout)

    def test_nested_symlink_fails_without_touching_link_target_or_unrelated_trees(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime = self._make_runtime(base)
            data = runtime / "shakemap/data"
            outside = base / "outside"
            outside.mkdir()
            outside_file = outside / "private.bin"
            outside_file.write_bytes(b"outside")
            outside_file.chmod(0o600)
            link = data / "regional/external"
            link.symlink_to(outside, target_is_directory=True)
            selected_safe = data / "global/safe.bin"
            selected_safe.write_bytes(b"safe")
            selected_safe.chmod(0o600)
            unrelated = {
                data / "inputs/private": b"input",
                runtime / "shakemap/products/private": b"product",
                runtime / "shakemap/logs/private": b"log",
                runtime / "shakemap/.service/private": b"state",
                data / "other/private": b"other",
            }
            for path, content in unrelated.items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o600)
            original_link_mode = self._mode(link, follow_symlinks=False)

            result = self._run(runtime)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe symbolic link in selected tree", result.stderr)
            self.assertIn(str(link), result.stderr)
            self.assertEqual(outside_file.read_bytes(), b"outside")
            self.assertEqual(self._mode(outside_file), 0o600)
            self.assertEqual(self._mode(link, follow_symlinks=False), original_link_mode)
            self.assertEqual(self._mode(selected_safe), 0o600)
            for path, content in unrelated.items():
                self.assertEqual(path.read_bytes(), content)
                self.assertEqual(self._mode(path), 0o600)

    def test_symlink_runtime_root_fails_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            real_runtime = self._make_runtime(base, "real-runtime")
            target = real_runtime / "shakemap/data/global/file"
            target.write_text("data", encoding="utf-8")
            target.chmod(0o600)
            linked_runtime = base / "linked-runtime"
            linked_runtime.symlink_to(real_runtime, target_is_directory=True)

            result = self._run(linked_runtime)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe symbolic-link directory", result.stderr)
            self.assertEqual(self._mode(target), 0o600)

    def test_special_entry_fails_before_safe_entries_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._make_runtime(Path(temporary))
            root = runtime / "shakemap/data/global"
            fifo = root / "unexpected.pipe"
            safe = root / "safe.bin"
            os.mkfifo(fifo, 0o600)
            safe.write_bytes(b"safe")
            safe.chmod(0o600)

            result = self._run(runtime)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Unsafe FIFO in selected tree", result.stderr)
            self.assertIn(str(fifo), result.stderr)
            self.assertEqual(self._mode(fifo), 0o600)
            self.assertEqual(self._mode(safe), 0o600)

    def test_missing_mandatory_ancestors_fail_without_creation_or_mode_changes(self) -> None:
        missing_paths = (
            Path("."),
            Path("shakemap"),
            Path("shakemap/data"),
        )
        for relative in missing_paths:
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                runtime = self._make_runtime(base)
                if relative == Path("."):
                    shutil.rmtree(runtime)
                    missing = runtime
                else:
                    missing = runtime / relative
                    if missing.is_dir():
                        shutil.rmtree(missing)
                existing_directories = [
                    path
                    for path in (
                        runtime,
                        runtime / "shakemap",
                        runtime / "shakemap/data",
                        *(runtime / "shakemap/data" / name for name in FIXED_NAMES),
                    )
                    if path.is_dir()
                ]
                for path in existing_directories:
                    path.chmod(0o700)
                original_modes = {
                    path: self._mode(path) for path in existing_directories
                }

                result = self._run(runtime)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(missing), result.stderr)
                self.assertFalse(missing.exists())
                self.assertEqual(
                    {path: self._mode(path) for path in existing_directories},
                    original_modes,
                )

    def test_missing_candidate_folders_are_skipped_while_present_folders_are_repaired(self) -> None:
        for missing_names in (("regional",), ("global", "regional")):
            with self.subTest(missing_names=missing_names), tempfile.TemporaryDirectory() as temporary:
                runtime = self._make_runtime(Path(temporary))
                data = runtime / "shakemap/data"
                for name in missing_names:
                    shutil.rmtree(data / name)
                targets = []
                for name in FIXED_NAMES:
                    root = data / name
                    if root.is_dir():
                        target = root / "copied.dat"
                        target.write_bytes(name.encode("utf-8"))
                        target.chmod(0o600)
                        targets.append(target)

                result = self._run(runtime)

                self.assertEqual(result.returncode, 0, result.stderr)
                for name in missing_names:
                    self.assertIn(f"SKIP missing data folder: {data / name}", result.stdout)
                    self.assertFalse((data / name).exists())
                for target in targets:
                    self.assertEqual(self._mode(target), 0o644)

    def test_all_candidate_folders_missing_fails_without_creation_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = self._make_runtime(Path(temporary))
            data = runtime / "shakemap/data"
            for name in FIXED_NAMES:
                shutil.rmtree(data / name)
            for path in (runtime, runtime / "shakemap", data):
                path.chmod(0o700)
            original_modes = {
                path: self._mode(path) for path in (runtime, runtime / "shakemap", data)
            }

            result = self._run(runtime)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("None of the default data folders exists", result.stderr)
            for name in FIXED_NAMES:
                self.assertFalse((data / name).exists())
            self.assertEqual(
                {path: self._mode(path) for path in original_modes},
                original_modes,
            )

    def test_invalid_existing_candidate_fails_before_mutation(self) -> None:
        for invalid_kind in ("file", "symlink"):
            with self.subTest(invalid_kind=invalid_kind), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                runtime = self._make_runtime(base)
                data = runtime / "shakemap/data"
                invalid = data / "regional"
                shutil.rmtree(invalid)
                outside = base / "outside"
                outside.mkdir()
                outside.chmod(0o700)
                if invalid_kind == "file":
                    invalid.write_bytes(b"not a directory")
                else:
                    invalid.symlink_to(outside, target_is_directory=True)
                invalid_mode = self._mode(invalid, follow_symlinks=False)
                safe = data / "global/copied.dat"
                safe.write_bytes(b"safe")
                safe.chmod(0o600)
                for path in (runtime, runtime / "shakemap", data, data / "global"):
                    path.chmod(0o700)
                original_modes = {
                    path: self._mode(path) for path in (runtime, runtime / "shakemap", data, data / "global", safe)
                }

                result = self._run(runtime)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(invalid), result.stderr)
                self.assertEqual(
                    {path: self._mode(path) for path in original_modes},
                    original_modes,
                )
                self.assertEqual(
                    self._mode(invalid, follow_symlinks=False),
                    invalid_mode,
                )
                if invalid_kind == "file":
                    self.assertEqual(invalid.read_bytes(), b"not a directory")
                self.assertEqual(self._mode(outside), 0o700)

    def test_rejects_unknown_options_and_positional_paths(self) -> None:
        cases = (
            ["--unknown"],
            ["--global-only"],
            ["/tmp/arbitrary"],
            ["--runtime-root"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    ["bash", str(HELPER), *arguments],
                    cwd=PROJECT,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)

    def test_chmod_failure_is_actionable_and_other_files_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            runtime = self._make_runtime(base)
            root = runtime / "shakemap/data/global"
            blocked = root / "blocked.bin"
            safe = root / "safe.bin"
            blocked.write_bytes(b"blocked")
            safe.write_bytes(b"safe")
            blocked.chmod(0o600)
            safe.chmod(0o600)
            fake_bin = base / "bin"
            fake_bin.mkdir()
            fake_chmod = fake_bin / "chmod"
            real_chmod = shutil.which("chmod")
            self.assertIsNotNone(real_chmod)
            fake_chmod.write_text(
                "#!/usr/bin/env bash\n"
                "last=''\n"
                "for value in \"$@\"; do last=\"$value\"; done\n"
                "if [[ \"$last\" == \"$FAIL_PATH\" ]]; then exit 73; fi\n"
                f'exec "{real_chmod}" "$@"\n',
                encoding="utf-8",
            )
            fake_chmod.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = os.pathsep.join((str(fake_bin), environment["PATH"]))
            environment["FAIL_PATH"] = str(blocked)

            result = self._run(runtime, environment=environment)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not apply chmod a+r", result.stderr)
            self.assertIn(str(blocked), result.stderr)
            self.assertIn(f"{blocked.stat().st_uid}:{blocked.stat().st_gid}", result.stderr)
            self.assertIn("Rerun as the path owner", result.stderr)
            self.assertEqual(self._mode(blocked), 0o600)
            self.assertEqual(self._mode(safe), 0o644)


if __name__ == "__main__":
    unittest.main()
