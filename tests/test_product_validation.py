from __future__ import annotations

import dataclasses
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import product_validation
from shakemap_service.required_products import RequiredProductResolution


def _snapshot_tree(root: Path) -> dict[str, tuple[object, ...]]:
    snapshot: dict[str, tuple[object, ...]] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if path.is_symlink():
            payload: object = os.readlink(path)
        elif path.is_file():
            payload = path.read_bytes()
        else:
            payload = None
        snapshot[relative] = (
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            payload,
        )
    return snapshot


class ProductValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.products = self.root / "products"
        self.products.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _resolution(
        paths: tuple[str, ...],
        source: str = "configured",
    ) -> RequiredProductResolution:
        return RequiredProductResolution(
            paths=paths,
            source=source,  # type: ignore[arg-type]
        )

    def test_order_duplicates_nested_paths_and_frozen_results(self) -> None:
        nested = self.products / "nested"
        nested.mkdir()
        (nested / "core.bin").write_bytes(b"core")
        (self.products / "other.dat").write_bytes(b"other")
        resolution = self._resolution(
            ("nested/core.bin", "other.dat", "nested/core.bin")
        )

        first = product_validation.validate_required_products(
            self.products,
            resolution,
        )
        second = product_validation.validate_required_products(
            self.products,
            resolution,
        )

        self.assertEqual(first.required_paths, resolution.paths)
        self.assertEqual(first.source, "configured")
        self.assertEqual(
            tuple(check.path for check in first.checks),
            resolution.paths,
        )
        self.assertEqual(
            tuple(check.size for check in first.checks),
            (4, 5, 4),
        )
        self.assertTrue(first.passed)
        self.assertTrue(all(check.passed for check in first.checks))
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        self.assertTrue(
            all(left is not right for left, right in zip(first.checks, second.checks))
        )
        self.assertEqual(
            tuple(field.name for field in dataclasses.fields(first.checks[0])),
            ("path", "size", "passed", "reason"),
        )
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.passed = False  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            first.checks[0].reason = "changed"  # type: ignore[misc]

    def test_invalid_resolver_input_never_touches_filesystem(self) -> None:
        invalid = (
            self._resolution(()),
            self._resolution(("safe.bin",), "other"),
            self._resolution(("",)),
            self._resolution(("/absolute",)),
            self._resolution(("a/../escape",)),
            self._resolution(("a//b",)),
            self._resolution(("line\nbreak",)),
        )
        for resolution in invalid:
            with self.subTest(resolution=resolution):
                with mock.patch.object(
                    Path,
                    "lstat",
                    side_effect=AssertionError("filesystem access attempted"),
                ):
                    with self.assertRaises(
                        product_validation.ProductValidationInputError
                    ):
                        product_validation.validate_required_products(
                            self.products,
                            resolution,
                        )

    def test_all_formats_receive_only_generic_checks(self) -> None:
        paths = (
            "shake_result.hdf",
            "map.jpg",
            "overlay.png",
            "grid.xml",
            "stations.json",
            "report.pdf",
            "opaque.bin",
        )
        for path in paths:
            (self.products / path).write_bytes(b"not valid format content")
        opened_modes: list[str] = []
        real_open = Path.open

        def record_open(path: Path, mode: str = "r", *args, **kwargs):
            opened_modes.append(mode)
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", new=record_open):
            result = product_validation.validate_required_products(
                self.products,
                self._resolution(paths),
            )

        self.assertTrue(result.passed)
        self.assertEqual(opened_modes, ["rb"] * len(paths))
        self.assertTrue(
            all(
                check.reason == "generic checks passed"
                for check in result.checks
            )
        )

    def test_artifact_failures_are_collected_and_later_entries_continue(self) -> None:
        (self.products / "empty.bin").touch()
        (self.products / "directory.bin").mkdir()
        (self.products / "present.bin").write_bytes(b"present")
        (self.products / "target.bin").write_bytes(b"target")
        (self.products / "link.bin").symlink_to("target.bin")
        (self.products / "unreadable.bin").write_bytes(b"denied")
        paths = (
            "missing.bin",
            "directory.bin",
            "link.bin",
            "unreadable.bin",
            "empty.bin",
            "present.bin",
        )
        real_open = Path.open

        def controlled_open(path: Path, mode: str = "r", *args, **kwargs):
            if path.name == "unreadable.bin":
                raise PermissionError(errno.EACCES, "permission denied", path)
            return real_open(path, mode, *args, **kwargs)

        with mock.patch.object(Path, "open", new=controlled_open):
            result = product_validation.validate_required_products(
                self.products,
                self._resolution(paths),
            )

        self.assertFalse(result.passed)
        self.assertEqual(tuple(check.path for check in result.checks), paths)
        reasons = {check.path: check.reason for check in result.checks}
        self.assertIn("missing", reasons["missing.bin"])
        self.assertIn("not a regular file", reasons["directory.bin"])
        self.assertIn("not a regular file", reasons["link.bin"])
        self.assertIn("unreadable", reasons["unreadable.bin"])
        self.assertIn("empty", reasons["empty.bin"])
        self.assertTrue(result.checks[-1].passed)
        self.assertEqual(result.checks[-1].size, 7)

    def test_missing_root_fails_each_entry_without_creation(self) -> None:
        missing_root = self.root / "missing-products"
        result = product_validation.validate_required_products(
            missing_root,
            self._resolution(("first.bin", "second.jpg")),
        )

        self.assertFalse(result.passed)
        self.assertEqual(len(result.checks), 2)
        self.assertTrue(
            all(check.reason == "product is missing" for check in result.checks)
        )
        self.assertFalse(missing_root.exists())

    def test_intermediate_symlink_cannot_escape_products_root(self) -> None:
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "escaped.bin").write_bytes(b"escaped")
        (self.products / "linked-directory").symlink_to(
            outside,
            target_is_directory=True,
        )
        (self.products / "later.bin").write_bytes(b"later")

        result = product_validation.validate_required_products(
            self.products,
            self._resolution(
                ("linked-directory/escaped.bin", "later.bin")
            ),
        )

        self.assertFalse(result.passed)
        self.assertIn("outside products directory", result.checks[0].reason)
        self.assertTrue(result.checks[1].passed)

    def test_stat_failure_is_collected_without_stopping(self) -> None:
        (self.products / "blocked.bin").write_bytes(b"blocked")
        (self.products / "later.bin").write_bytes(b"later")
        real_lstat = Path.lstat

        def controlled_lstat(path: Path):
            if path.name == "blocked.bin":
                raise PermissionError(errno.EACCES, "permission denied", path)
            return real_lstat(path)

        with mock.patch.object(Path, "lstat", new=controlled_lstat):
            result = product_validation.validate_required_products(
                self.products,
                self._resolution(("blocked.bin", "later.bin")),
            )

        self.assertFalse(result.passed)
        self.assertIn("could not be inspected", result.checks[0].reason)
        self.assertTrue(result.checks[1].passed)

    def test_validation_does_not_mutate_filesystem_or_environment(self) -> None:
        (self.products / "core.bin").write_bytes(b"core")
        inputs = self.root / "inputs"
        inputs.mkdir()
        (inputs / "event.xml").write_bytes(b"event")
        status = self.root / "status.json"
        status.write_text('{"status":"RUNNING"}', encoding="utf-8")
        before_tree = _snapshot_tree(self.root)
        before_environment = dict(os.environ)

        result = product_validation.validate_required_products(
            self.products,
            self._resolution(("core.bin",)),
        )

        self.assertTrue(result.passed)
        self.assertEqual(_snapshot_tree(self.root), before_tree)
        self.assertEqual(dict(os.environ), before_environment)


if __name__ == "__main__":
    unittest.main()
