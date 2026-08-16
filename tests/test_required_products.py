from __future__ import annotations

import builtins
import dataclasses
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import required_products
from shakemap_service.config import Settings


class _HdfFile:
    def __init__(
        self,
        source: object,
        mode: str,
        close_error: Exception | None = None,
    ) -> None:
        self.source = source
        self.mode = mode
        self.close_error = close_error
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class _NativeContainer:
    def __init__(self, imts: object) -> None:
        self.imts = imts
        self.closed = False
        self.hdf_files: list[_HdfFile] = []
        self.read_error: Exception | None = None
        self.close_error: Exception | None = None

    def getIMTs(self) -> object:
        if self.read_error is not None:
            raise self.read_error
        return self.imts

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


def _native_modules(
    *,
    container: _NativeContainer | None = None,
    hdf_open_error: Exception | None = None,
    construct_error: Exception | None = None,
    hdf_close_error: Exception | None = None,
    converter=None,
) -> dict[str, types.ModuleType]:
    h5py_module = types.ModuleType("h5py")
    opened_files: list[_HdfFile] = []

    def open_hdf(source: object, mode: str) -> _HdfFile:
        if hdf_open_error is not None:
            raise hdf_open_error
        hdf_file = _HdfFile(source, mode, hdf_close_error)
        opened_files.append(hdf_file)
        return hdf_file

    h5py_module.File = open_hdf
    h5py_module.opened_files = opened_files

    io_package = types.ModuleType("esi_utils_io")
    containers_module = types.ModuleType("esi_utils_io.smcontainers")

    class CompatibleReader:
        def __new__(cls, hdf_file: _HdfFile) -> _NativeContainer:
            if construct_error is not None:
                raise construct_error
            if container is None:
                raise AssertionError("test container is missing")
            container.hdf_files.append(hdf_file)
            return container

        @classmethod
        def load(cls, path: str) -> _NativeContainer:
            raise AssertionError(f"write-capable load attempted: {path}")

    containers_module.ShakeMapOutputContainer = CompatibleReader
    io_package.smcontainers = containers_module

    shakelib_package = types.ModuleType("esi_shakelib")
    utils_package = types.ModuleType("esi_shakelib.utils")
    imt_module = types.ModuleType("esi_shakelib.utils.imt_string")
    imt_module.oq_to_file = converter or (lambda imt: imt.lower())
    utils_package.imt_string = imt_module
    shakelib_package.utils = utils_package
    return {
        "h5py": h5py_module,
        "esi_utils_io": io_package,
        "esi_utils_io.smcontainers": containers_module,
        "esi_shakelib": shakelib_package,
        "esi_shakelib.utils": utils_package,
        "esi_shakelib.utils.imt_string": imt_module,
    }


class RequiredProductTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.products = Path(self.temporary.name) / "current" / "products"
        self.previous_settings = required_products.settings

    def tearDown(self) -> None:
        required_products.settings = self.previous_settings
        self.temporary.cleanup()

    def _configure(self, paths: tuple[str, ...] = ()) -> None:
        required_products.settings = Settings(required_products=paths)

    def test_nonempty_configuration_is_exact_and_never_imports_native_code(
        self,
    ) -> None:
        configured = (
            "custom/core.bin",
            "duplicate.dat",
            "duplicate.dat",
            r"literal\name",
        )
        self._configure(configured)
        imported: list[str] = []
        real_import = builtins.__import__

        def reject_native_import(name, *args, **kwargs):
            if name.startswith("esi_"):
                imported.append(name)
                raise AssertionError(f"native import attempted: {name}")
            return real_import(name, *args, **kwargs)

        with mock.patch.object(builtins, "__import__", new=reject_native_import):
            resolution = required_products.resolve_required_products(self.products)

        self.assertEqual(resolution.paths, configured)
        self.assertEqual(resolution.source, "configured")
        self.assertEqual(imported, [])
        self.assertFalse(self.products.parent.exists())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            resolution.source = "derived"  # type: ignore[misc]

    def test_configured_paths_must_be_canonical_safe_posix_relative_paths(
        self,
    ) -> None:
        unsafe = (
            "",
            "/absolute",
            ".",
            "..",
            "a/./b",
            "a/../b",
            "a//b",
            "a/",
            "line\nbreak",
            "nul\x00byte",
            "control\x85byte",
        )
        real_import = builtins.__import__

        def reject_native_import(name, *args, **kwargs):
            if name.startswith("esi_"):
                raise AssertionError(f"native import attempted: {name}")
            return real_import(name, *args, **kwargs)

        for path in unsafe:
            with self.subTest(path=path):
                self._configure((path,))
                with mock.patch.object(
                    builtins,
                    "__import__",
                    new=reject_native_import,
                ):
                    with self.assertRaises(
                        required_products.RequiredProductResolutionError
                    ):
                        required_products.resolve_required_products(self.products)
        self.assertFalse(self.products.parent.exists())

    def test_empty_configuration_derives_unique_release_native_rasters(
        self,
    ) -> None:
        self._configure()
        container = _NativeContainer(
            (
                "GREATER_OF_TWO_HORIZONTAL/SA(3.0)",
                "ROTD50/PGA",
                "GREATER_OF_TWO_HORIZONTAL/MMI",
                "GREATER_OF_TWO_HORIZONTAL/PGA",
                "GREATER_OF_TWO_HORIZONTAL/SA(0.3)",
                "GREATER_OF_TWO_HORIZONTAL/PGV",
                "GREATER_OF_TWO_HORIZONTAL/SA(1.0)",
            )
        )
        converted: list[str] = []
        names = {
            "PGA": "pga",
            "PGV": "pgv",
            "SA(0.3)": "psa0p3",
            "SA(1.0)": "psa1p0",
            "SA(3.0)": "psa3p0",
        }

        def convert(imt: str) -> str:
            converted.append(imt)
            return names[imt]

        modules = _native_modules(container=container, converter=convert)
        tree_before = set(Path(self.temporary.name).rglob("*"))
        with mock.patch.dict("sys.modules", modules):
            resolution = required_products.resolve_required_products(self.products)

        self.assertEqual(
            resolution.paths,
            (
                "shake_result.hdf",
                "intensity.jpg",
                "pga.jpg",
                "pgv.jpg",
                "psa0p3.jpg",
                "psa1p0.jpg",
                "psa3p0.jpg",
            ),
        )
        self.assertEqual(resolution.source, "derived")
        self.assertEqual(converted, list(names))
        self.assertTrue(container.closed)
        self.assertEqual(len(container.hdf_files), 1)
        self.assertEqual(
            container.hdf_files[0].source,
            str(self.products / "shake_result.hdf"),
        )
        self.assertEqual(container.hdf_files[0].mode, "r")
        self.assertTrue(container.hdf_files[0].closed)
        self.assertEqual(set(Path(self.temporary.name).rglob("*")), tree_before)
        self.assertNotIn("mmi_legend.png", resolution.paths)
        self.assertNotIn("intensity_overlay.png", resolution.paths)
        self.assertNotIn("intensity.pdf", resolution.paths)

    def test_hdf_open_construct_read_and_close_failures_are_confined(
        self,
    ) -> None:
        self._configure()

        modules = _native_modules(hdf_open_error=OSError("open failed"))
        with mock.patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(
                required_products.RequiredProductResolutionError,
                "open failed",
            ):
                required_products.resolve_required_products(self.products)
        self.assertEqual(modules["h5py"].opened_files, [])

        modules = _native_modules(construct_error=ValueError("construct failed"))
        with mock.patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(
                required_products.RequiredProductResolutionError,
                "construct failed",
            ):
                required_products.resolve_required_products(self.products)
        self.assertTrue(modules["h5py"].opened_files[0].closed)

        read_failure = _NativeContainer(())
        read_failure.read_error = ValueError("read failed")
        read_failure.close_error = OSError("reader close also failed")
        modules = _native_modules(
            container=read_failure,
            hdf_close_error=OSError("file close also failed"),
        )
        with mock.patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(
                required_products.RequiredProductResolutionError,
                "read failed",
            ):
                required_products.resolve_required_products(self.products)
        self.assertTrue(read_failure.closed)
        self.assertTrue(modules["h5py"].opened_files[0].closed)

        close_failure = _NativeContainer(("COMP/PGA",))
        close_failure.close_error = OSError("close failed")
        modules = _native_modules(
            container=close_failure,
            converter=lambda imt: imt.lower(),
        )
        with mock.patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(
                required_products.RequiredProductResolutionError,
                "close failed",
            ):
                required_products.resolve_required_products(self.products)
        self.assertTrue(close_failure.closed)
        self.assertTrue(modules["h5py"].opened_files[0].closed)

        file_close_failure = _NativeContainer(("COMP/PGA",))
        modules = _native_modules(
            container=file_close_failure,
            hdf_close_error=OSError("file close failed"),
            converter=lambda imt: imt.lower(),
        )
        with mock.patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(
                required_products.RequiredProductResolutionError,
                "file close failed",
            ):
                required_products.resolve_required_products(self.products)
        self.assertTrue(file_close_failure.closed)
        self.assertTrue(modules["h5py"].opened_files[0].closed)
        self.assertFalse(self.products.parent.exists())

    def test_invalid_or_empty_native_imt_inventory_fails_after_close(self) -> None:
        self._configure()
        inventories = (
            (),
            ("PGA",),
            ("/PGA",),
            ("COMP/",),
            ("COMP/PGA/extra",),
            (" COMP/PGA",),
            ("COMP/PGA ",),
            (1,),
        )
        for inventory in inventories:
            with self.subTest(inventory=inventory):
                container = _NativeContainer(inventory)
                modules = _native_modules(container=container)
                with mock.patch.dict("sys.modules", modules):
                    with self.assertRaises(
                        required_products.RequiredProductResolutionError
                    ):
                        required_products.resolve_required_products(self.products)
                self.assertTrue(container.closed)
                self.assertTrue(modules["h5py"].opened_files[0].closed)

    def test_converter_failures_and_ambiguous_names_do_not_fallback(self) -> None:
        self._configure()

        def conversion_failure(imt: str) -> str:
            raise ValueError(f"unsupported {imt}")

        cases = (
            (("COMP/PGA",), conversion_failure, "could not map"),
            (("COMP/PGA",), lambda imt: "", "unambiguous"),
            (("COMP/PGA",), lambda imt: "../escape", "unambiguous"),
            (
                ("COMP/PGA", "COMP/PGV"),
                lambda imt: "same",
                "ambiguously",
            ),
        )
        for inventory, converter, message in cases:
            with self.subTest(message=message):
                container = _NativeContainer(inventory)
                modules = _native_modules(
                    container=container,
                    converter=converter,
                )
                with mock.patch.dict("sys.modules", modules):
                    with self.assertRaisesRegex(
                        required_products.RequiredProductResolutionError,
                        message,
                    ):
                        required_products.resolve_required_products(self.products)
                self.assertTrue(container.closed)

    def test_native_configuration_files_do_not_override_hdf_inventory(self) -> None:
        self._configure()
        native_current = self.products.parent
        native_current.mkdir(parents=True)
        (native_current / "model.conf").write_text(
            "imt_list = MMI",
            encoding="utf-8",
        )
        (native_current / "model_select.conf").write_text(
            "imt_list = PGV",
            encoding="utf-8",
        )
        before = {
            path: path.read_bytes()
            for path in native_current.iterdir()
        }
        container = _NativeContainer(("COMP/SA(0.01)",))
        modules = _native_modules(
            container=container,
            converter=lambda imt: "psa0p01",
        )
        with mock.patch.dict("sys.modules", modules):
            first = required_products.resolve_required_products(self.products)
        with mock.patch.dict("sys.modules", modules):
            second = required_products.resolve_required_products(self.products)

        self.assertEqual(
            first,
            required_products.RequiredProductResolution(
                paths=("shake_result.hdf", "psa0p01.jpg"),
                source="derived",
            ),
        )
        self.assertIsNot(first, second)
        self.assertEqual(first, second)
        self.assertEqual(
            {path: path.read_bytes() for path in native_current.iterdir()},
            before,
        )
        self.assertTrue(
            all(hdf_file.closed for hdf_file in modules["h5py"].opened_files)
        )


if __name__ == "__main__":
    unittest.main()
