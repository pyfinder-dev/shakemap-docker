"""Prove that recovery evidence comes from native types and loading boundaries."""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import native_launcher as launcher


def captured_error(module, source, *arguments):
    namespace = {"__name__": module}
    exec(compile(source, "native_fixture.py", "exec"), namespace)
    try:
        namespace["invoke"](*arguments)
    except Exception as error:
        return error
    raise AssertionError("fixture did not fail")


FACTORY = '''
def get_object_from_config(obj, section, cfg, error):
    mpath, cname = cfg
    raise error

def invoke(cfg, error):
    get_object_from_config("gmice", "modeling", cfg, error)
'''
VALIDATION = '''
def config_error(config, results):
    errs = 1
    raise RuntimeError("native validation failed")

def invoke(config):
    config_error(config, {"data": {"vs30file": False}})
'''


class NativeLauncherTests(unittest.TestCase):
    def test_missing_selected_module_or_parent_is_classified(self):
        for missing in ("shakelib", "shakelib.gmice", "shakelib.gmice.ofm22"):
            error = captured_error(
                launcher.FACTORY_MODULE, FACTORY,
                ("shakelib.gmice.ofm22", "OFM22"),
                ModuleNotFoundError("missing", name=missing),
            )
            self.assertEqual(launcher.configuration_error(error)["reference"], "shakelib.gmice.ofm22")

    def test_dependency_and_constructor_failures_stay_generic(self):
        for error in (
            ModuleNotFoundError("dependency missing", name="numpy"),
            AttributeError("constructor failed", name="coefficient", obj=object()),
            ValueError("bad calculation"),
            FileNotFoundError("output missing"),
            PermissionError("output denied"),
        ):
            caught = captured_error(launcher.FACTORY_MODULE, FACTORY, ("custom.gmice", "Custom"), error)
            self.assertIsNone(launcher.configuration_error(caught))

    def test_missing_configured_class_is_distinct_from_constructor_attribute(self):
        module = types.ModuleType("custom.gmice")
        error = captured_error(
            launcher.FACTORY_MODULE, FACTORY, ("custom.gmice", "Custom"),
            AttributeError("missing class", name="Custom", obj=module),
        )
        self.assertEqual(launcher.configuration_error(error)["class_name"], "Custom")

    def test_native_validation_requires_private_profile_file(self):
        with mock.patch.dict(os.environ, {"HOME": "/service/profile/home"}):
            for filename, recognized in (
                ("/service/profile/install/config/model.conf", True),
                ("/service/profile/install/config/products.conf", True),
                ("/events/event.xml", False),
                ("/elsewhere/model.conf", False),
            ):
                error = captured_error(launcher.CONFIG_MODULE, VALIDATION, types.SimpleNamespace(filename=filename))
                fact = launcher.configuration_error(error)
                self.assertEqual(fact is not None, recognized)
                if recognized:
                    self.assertEqual(fact["invalid_fields"], ["data.vs30file"])
                    self.assertEqual(fact["reference"], Path(filename).name)

    def test_same_exception_in_unrelated_code_is_not_configuration_failure(self):
        error = captured_error("unrelated.model", VALIDATION, types.SimpleNamespace(filename="/model.conf"))
        self.assertIsNone(launcher.configuration_error(error))

    def test_launcher_preserves_exit_formatter_and_arguments(self):
        error = captured_error(
            launcher.FACTORY_MODULE, FACTORY, ("custom.gmice", "Custom"),
            ModuleNotFoundError("missing", name="custom.gmice"),
        )
        formatter = mock.Mock(return_value="original log text")
        native = types.SimpleNamespace(_format_error_info=formatter)

        def native_main():
            self.assertEqual(launcher.sys.argv, ["launcher", "evt", "select", "model"])
            self.assertEqual(native._format_error_info(error, "evt"), "original log text")
            raise SystemExit(1)

        native.main = native_main
        with tempfile.TemporaryDirectory() as temporary:
            report = Path(temporary) / "failure.json"
            with (
                mock.patch.object(launcher.importlib, "import_module", return_value=native),
                mock.patch.object(launcher.sys, "argv", ["launcher", "evt", "select", "model"]),
                mock.patch.dict(os.environ, {launcher.REPORT_ENV: str(report), launcher.TOKEN_ENV: "token"}),
            ):
                with self.assertRaises(SystemExit) as raised:
                    launcher.main()
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(json.loads(report.read_text())["event_id"], "evt")
        self.assertIs(native._format_error_info, formatter)
        formatter.assert_called_once_with(error, "evt")

    def test_missing_hook_fails_before_native_main(self):
        native = types.SimpleNamespace(main=mock.Mock())
        with mock.patch.object(launcher.importlib, "import_module", return_value=native):
            with self.assertRaisesRegex(RuntimeError, "hook is unavailable"):
                launcher.main()
        native.main.assert_not_called()
