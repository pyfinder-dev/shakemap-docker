"""Run official ShakeMap while retaining narrowly typed configuration errors.

The supported native CLI catches exceptions before returning its exit code.
Its error-formatting hook is the last place where the original exception and
traceback remain available. We observe that hook without changing model code,
configuration validation, logging, or the native decision to fail.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import FrameType
from typing import Any


REPORT_ENV = "SHAKEMAP_NATIVE_FAILURE_REPORT"
TOKEN_ENV = "SHAKEMAP_NATIVE_FAILURE_TOKEN"
CONFIG_MODULE = "shakemap_modules.utils.config"
FACTORY_MODULE = "shakemap_modules.utils.utils"
CONFIG_FILES = frozenset({
    "model.conf", "modules.conf", "gmpe_sets.conf", "select.conf", "products.conf",
})


def _profile_config(frame: FrameType, name: str) -> str | None:
    """Accept only the private profile's native configuration file identities."""
    config = frame.f_locals.get(name)
    filename = getattr(config, "filename", None)
    home = os.environ.get("HOME")
    if not isinstance(filename, str) or not home:
        return None
    path = Path(filename)
    expected = Path(home).parent / "install" / "config"
    if path.parent != expected or path.name not in CONFIG_FILES:
        return None
    # The service's public records omit private profile paths. The selected
    # configuration and its copied file hashes identify this native file.
    return path.name


def configuration_error(error: BaseException) -> dict[str, Any] | None:
    """Recognize failures at exact native boundaries, never from log wording.

    In particular, a missing dependency inside a successfully selected custom
    module is not the same evidence as a missing configured module. Disk errors,
    model arithmetic and arbitrary missing event/output files remain generic.
    """
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        module = frame.f_globals.get("__name__")
        function = frame.f_code.co_name
        values = frame.f_locals

        if module == CONFIG_MODULE and function == "config_error":
            reference = _profile_config(frame, "config")
            # This native function raises RuntimeError only after ConfigObj
            # has reported invalid fields or missing sections.
            if (
                reference
                and type(error) is RuntimeError
                and values.get("errs", 0) > 0
            ):
                fact = {
                    "origin": "native_config_validation",
                    "exception_type": "RuntimeError",
                    "reference": reference,
                }
                # ConfigObj already evaluated these fields during execution.
                # Retain its failed key names without running validation again
                # or publishing arbitrary configuration values.
                failed_fields: list[str] = []

                def collect(result: Any, prefix: str = "") -> None:
                    if isinstance(result, dict):
                        for key, value in result.items():
                            collect(value, f"{prefix}.{key}" if prefix else str(key))
                    elif result is not True:
                        failed_fields.append(prefix)

                collect(values.get("results"))
                fact["invalid_fields"] = failed_fields
                return fact

        if module == CONFIG_MODULE and function == "check_config":
            reference = _profile_config(frame, "config")
            if (
                reference
                and type(error).__module__ == "validate"
                and type(error).__name__ == "ValidateError"
            ):
                return {
                    "origin": "native_model_reference",
                    "exception_type": "ValidateError",
                    "reference": reference,
                }

        if module == FACTORY_MODULE and function == "get_object_from_config":
            requested = values.get("mpath")
            classname = values.get("cname")
            if (
                not isinstance(requested, str)
                or len(requested) > 512
                or not all(part.isidentifier() for part in requested.split("."))
                or not isinstance(classname, str)
                or not classname.isidentifier()
                or values.get("obj") not in {"gmice", "ipe", "ccf"}
            ):
                traceback = traceback.tb_next
                continue
            missing_module = (
                isinstance(error, ModuleNotFoundError)
                and isinstance(error.name, str)
                and (requested == error.name or requested.startswith(error.name + "."))
            )
            # AttributeError supplies the actual module object on modern
            # Python; constructor AttributeErrors must not enter this branch.
            missing_class = (
                isinstance(error, AttributeError)
                and error.name == classname
                and getattr(error.obj, "__name__", None) == requested
            )
            if missing_module or missing_class:
                return {
                    "origin": "native_configured_module",
                    "exception_type": type(error).__name__,
                    "reference": requested,
                    "class_name": classname,
                    "model_kind": values.get("obj"),
                }
        traceback = traceback.tb_next
    return None


def main() -> Any:
    """Delegate the original argument list and preserve its native exit policy."""
    native = importlib.import_module("shakemap.bin.shake")
    formatter = getattr(native, "_format_error_info", None)
    if not callable(formatter):
        raise RuntimeError("supported ShakeMap error-reporting hook is unavailable")

    def retain_error(error: BaseException, event_id: str) -> str:
        try:
            fact = configuration_error(error)
            if fact is not None:
                report = {
                    "event_id": event_id,
                    "token": os.environ[TOKEN_ENV],
                    "configuration_error": fact,
                }
                # Exclusive creation prevents accidental reuse of a previous
                # execution's evidence. Reporting failure never changes the
                # original native error, nor invents eligibility for recovery.
                with open(os.environ[REPORT_ENV], "x", encoding="utf-8") as stream:
                    json.dump(report, stream)
        except Exception as reporting_error:
            # Diagnostic failure must not prevent the official formatter from
            # logging the original native exception or changing its exit path.
            print(f"Native failure evidence unavailable: {reporting_error}", file=sys.stderr)
        return formatter(error, event_id)

    native._format_error_info = retain_error
    try:
        return native.main()
    finally:
        native._format_error_info = formatter


if __name__ == "__main__":
    sys.exit(main())
