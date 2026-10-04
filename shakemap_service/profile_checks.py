"""Read-only prerequisite diagnostics for an explicitly selected native profile.

This is an operator tool, never a submission preflight. It checks references
without constructing models, materializing profiles, or deciding scientific
suitability. A completed static check cannot promise a successful calculation.
"""

from __future__ import annotations

import importlib.machinery
import importlib.metadata
import os
from pathlib import Path
import stat

from configobj import ConfigObj, ConfigObjError

from . import paths
from .native_profile import REGIONAL_CONFIGURATION_FILES
from .request_validation import validate_configuration_name

MAX_CONFIG_BYTES = 1024 * 1024
LIMITS = [
    "Native execution was not run; static checks cannot anticipate every failure.",
    "Scientific suitability, geographic coverage, model construction, and full native schema validation are not assessed.",
    "Selection branches are inspected without choosing an event geography.",
]


def _native_data_directory() -> Path | None:
    """Locate installed templates without importing the scientific stack."""
    try:
        package = importlib.metadata.distribution("shakemap-modules")
    except importlib.metadata.PackageNotFoundError:
        return None
    return Path(package.locate_file("shakemap_modules/data"))


def _module_available(name: str) -> bool | None:
    """Resolve normal package paths without executing package or model code.

    Custom import hooks and dynamic aliases are outside this static probe. An
    uninspectable parent produces UNKNOWN instead of a fabricated success.
    """
    if not name or not all(part.isidentifier() for part in name.split(".")):
        return None

    search = None
    for index in range(1, len(name.split(".")) + 1):
        partial = ".".join(name.split(".")[:index])
        try:
            spec = importlib.machinery.PathFinder.find_spec(partial, search)
        except (ImportError, AttributeError, OSError, ValueError):
            return None
        if spec is None:
            return False
        search = spec.submodule_search_locations
        if index < len(name.split(".")) and search is None:
            return False
    return True


class _Check:
    """Accumulate one bounded report; there is no persistent check state."""

    def __init__(self, configuration: str):
        self.configuration = configuration
        self.findings = []
        self.native_data = _native_data_directory()
        self.source = (
            self.native_data
            if configuration == "global"
            else paths.regional_data_dir() / configuration
        )
        self.documents = {}

    def add(
        self,
        status,
        code,
        reason,
        action=None,
        *,
        filename=None,
        section=None,
        key=None,
        reference=None,
        resolved=None,
    ):
        self.findings.append(
            {
                "status": status,
                "code": code,
                "configuration": self.configuration,
                "config_file": (
                    str(self.source / filename) if self.source and filename else None
                ),
                "section": section,
                "key": key,
                "reference": reference,
                "resolved_path": str(resolved) if resolved is not None else None,
                "reason": reason,
                "corrective_action": action,
                "evidence": "static",
            }
        )

    def file(
        self,
        target,
        *,
        filename=None,
        section=None,
        key=None,
        reference=None,
        directory=False,
    ):
        """Keep missing, unreadable, and wrong-kind failures distinguishable."""
        context = dict(
            filename=filename,
            section=section,
            key=key,
            reference=reference,
            resolved=target,
        )
        try:
            metadata = target.stat()
        except ValueError:
            self.add(
                "BROKEN",
                "invalid_path_reference",
                "Configured path is not a valid filesystem reference.",
                "Review this exact path setting; no file was changed.",
                **context,
            )
            return False
        except FileNotFoundError:
            self.add(
                "MISSING",
                "path_missing",
                "Referenced path does not exist.",
                "Correct this reference or provide the intended asset at a readable mounted path; rerun check.",
                **context,
            )
            return False
        except OSError:
            self.add(
                "UNKNOWN",
                "path_unavailable",
                "Path metadata could not be read.",
                "Check mount and directory traversal permissions for the service user; rerun check.",
                **context,
            )
            return False

        correct_type = (
            stat.S_ISDIR(metadata.st_mode)
            if directory
            else stat.S_ISREG(metadata.st_mode)
        )
        if not correct_type:
            self.add(
                "BROKEN",
                "path_type",
                "Referenced path has the wrong file type.",
                "Review the configured path; supply the intended directory or regular file without replacing unrelated data.",
                **context,
            )
            return False
        access = os.R_OK | (os.X_OK if directory else 0)
        if not os.access(target, access):
            self.add(
                "BROKEN",
                "path_unreadable",
                "The service user cannot read this path.",
                "Grant the service user read and directory traversal access; keep the asset in place and rerun check.",
                **context,
            )
            return False
        self.add(
            "OK",
            "path_readable",
            "Path exists and is readable; contents and suitability are not validated.",
            **context,
        )
        return True

    def read_documents(self):
        if self.source is None:
            self.add(
                "UNKNOWN",
                "native_templates_unavailable",
                "Installed ShakeMap templates could not be located.",
                "Check the installed image and its shakemap-modules package.",
            )
            return

        for filename in REGIONAL_CONFIGURATION_FILES:
            target = self.source / filename
            if not self.file(target, filename=filename):
                continue
            try:
                # Bound the input and disable interpolation. Parser error text
                # can contain unrelated configuration values, so never echo it.
                with target.open("rb") as stream:
                    content = stream.read(MAX_CONFIG_BYTES + 1)
                if len(content) > MAX_CONFIG_BYTES:
                    raise ValueError("configuration exceeds size limit")
                document = ConfigObj(
                    content.decode("utf-8-sig").splitlines(),
                    interpolation=False,
                    raise_errors=True,
                )
            except (OSError, UnicodeError, ConfigObjError, ValueError) as error:
                self.add(
                    "BROKEN",
                    "configuration_parse_failed",
                    f"Cannot parse bounded native configuration ({type(error).__name__}).",
                    "Review this file's native syntax and readability; no file was changed.",
                    filename=filename,
                )
                continue
            self.documents[filename] = document

    def value(self, filename, section, key):
        document = self.documents.get(filename)
        if document is None:
            return None
        current = document
        for part in section:
            current = current.get(part) if isinstance(current, dict) else None
        value = current.get(key) if isinstance(current, dict) else None
        if value is None or isinstance(value, dict):
            self.add(
                "UNKNOWN",
                "configuration_key_unavailable",
                "Expected configuration key is absent or is not a scalar/list value.",
                "Review the native configuration for this installed release; defaults are not guessed.",
                filename=filename,
                section=section,
                key=key,
            )
            return None
        return value

    def path_reference(self, filename, section, key, value, *, directory=False):
        if not isinstance(value, str) or not value.strip():
            self.add(
                "BROKEN",
                "invalid_path_reference",
                "Expected a nonempty path value.",
                "Review this native path setting.",
                filename=filename,
                section=section,
                key=key,
            )
            return None
        if value == "None":
            self.add(
                "UNKNOWN",
                "disabled_path_reference",
                "No file is selected; the native default or disabled behavior is not validated here.",
                "Confirm the intended native behavior with the profile owner.",
                filename=filename,
                section=section,
                key=key,
                reference=value,
            )
            return None

        # sm_profile copies packaged layers into each private installation. We
        # inspect that exact source instead of inventing a future event path.
        if value == "<INSTALL_DIR>/data/layers" and self.native_data:
            target = self.native_data / "layers"
        elif "<" in value or not Path(value).is_absolute():
            self.add(
                "UNKNOWN",
                "unresolved_path_reference",
                "This path requires a private profile or unsupported path expansion.",
                "Review its native resolution and mounts; the check does not create a profile.",
                filename=filename,
                section=section,
                key=key,
                reference=value,
            )
            return None
        else:
            target = Path(value)
        self.file(
            target,
            filename=filename,
            section=section,
            key=key,
            reference=value,
            directory=directory,
        )
        return target

    def check_paths(self):
        for filename, section, key, global_target in (
            ("model.conf", ["data"], "vs30file", paths.vs30_grid_path()),
            (
                "products.conf",
                ["products", "mapping", "layers"],
                "topography",
                paths.topo_grid_path(),
            ),
        ):
            value = self.value(filename, section, key)
            if value is not None:
                # The materializer explicitly replaces these two global values.
                effective = (
                    str(global_target) if self.configuration == "global" else value
                )
                self.path_reference(filename, section, key, effective)

        value = self.value("select.conf", ["layers"], "layer_dir")
        if value is not None:
            target = self.path_reference(
                "select.conf", ["layers"], "layer_dir", value, directory=True
            )
            layers = self.documents.get("select.conf", {}).get("layers", {})
            if target and isinstance(layers, dict):
                for name, definition in layers.items():
                    if isinstance(definition, dict):
                        if Path(name).name != name or name in {".", ".."}:
                            self.add(
                                "BROKEN",
                                "invalid_layer_name",
                                "Layer name is not a simple filename.",
                                "Review the native layer name.",
                                filename="select.conf",
                                section=["layers", name],
                            )
                            continue
                        self.file(
                            target / f"{name}.wkt",
                            filename="select.conf",
                            section=["layers", name],
                            key="layer_dir",
                            reference=value,
                        )

        model = self.documents.get("model.conf", {}).get("modeling", {})
        enabled = (
            model.get("apply_generic_amp_factors") if isinstance(model, dict) else None
        )
        if isinstance(enabled, str) and enabled.lower() in {"true", "yes", "1", "on"}:
            self.add(
                "UNKNOWN",
                "amplification_not_materialized",
                "Generic amplification is enabled, but the current materializer does not copy operator amplification files into the private profile.",
                "Review the intended amplification files and profile wiring with the profile owner; presence elsewhere does not establish use.",
                filename="model.conf",
                section=["modeling"],
                key="apply_generic_amp_factors",
                reference=enabled,
            )

    def check_models(self):
        modules = self.documents.get("modules.conf")
        if modules is None:
            return
        used = set()

        def walk(filename, node, section):
            if not isinstance(node, dict):
                return
            for key, value in node.items():
                if isinstance(value, dict):
                    walk(filename, value, section + [key])
                elif key in {"gmpe", "gmice", "ipe", "ccf"}:
                    for alias in value if isinstance(value, list) else [value]:
                        used.add((key, str(alias), filename, tuple(section)))

        for filename in ("model.conf", "select.conf"):
            walk(filename, self.documents.get(filename), [])
        for key in ("gmpe", "gmice", "ipe", "ccf"):
            self.value("model.conf", ["modeling"], key)

        # Selection refers to GMPE sets, whose component aliases are declared
        # in modules.conf. Follow those references without evaluating weights
        # or selecting a tectonic/geographic branch.
        sets = self.documents.get("gmpe_sets.conf", {}).get("gmpe_sets", {})
        for kind, alias, filename, section in sorted(used.copy()):
            if kind != "gmpe":
                continue
            used.remove((kind, alias, filename, section))
            definition = sets.get(alias) if isinstance(sets, dict) else None
            if not isinstance(definition, dict):
                self.add(
                    "BROKEN",
                    "gmpe_set_missing",
                    "Selected GMPE set is absent from gmpe_sets.conf.",
                    "Review this set reference against the intended native configuration.",
                    filename=filename,
                    section=list(section),
                    key=kind,
                    reference=alias,
                )
                continue
            for key in ("gmpes", "site_gmpes"):
                components = definition.get(key)
                if components is None:
                    if key == "gmpes":
                        self.add(
                            "UNKNOWN",
                            "gmpe_components_unavailable",
                            "The selected GMPE set has no component list.",
                            "Review this native GMPE set definition.",
                            filename="gmpe_sets.conf",
                            section=["gmpe_sets", alias],
                            key=key,
                        )
                    continue
                for component in (
                    components if isinstance(components, list) else [components]
                ):
                    if component != "None":
                        used.add(
                            (
                                "gmpe",
                                str(component),
                                "gmpe_sets.conf",
                                ("gmpe_sets", alias),
                            )
                        )

        for kind, alias, filename, section in sorted(used):
            definitions = modules.get(f"{kind}_modules", {})
            definition = (
                definitions.get(alias) if isinstance(definitions, dict) else None
            )
            if not isinstance(definition, list) or len(definition) != 2:
                self.add(
                    "BROKEN",
                    "model_reference_missing",
                    "Selected model alias has no class/module pair in modules.conf.",
                    "Review the selected alias and its native module definition.",
                    filename=filename,
                    section=list(section),
                    key=kind,
                    reference=alias,
                )
                continue
            class_name, module = definition
            available = _module_available(module)
            self.add(
                "OK" if available else "MISSING" if available is False else "UNKNOWN",
                "model_module_available" if available else "model_module_unavailable",
                (
                    "Module path is discoverable; its class, dependencies and constructor were not executed."
                    if available
                    else "Configured module cannot be resolved by static package lookup; dynamic import hooks are not evaluated."
                ),
                (
                    None
                    if available
                    else "Review this model definition against the installed image; install only the profile owner's intended compatible implementation."
                ),
                filename="modules.conf",
                section=[f"{kind}_modules"],
                key=alias,
                reference=f"{class_name}, {module}",
            )


def check_configuration(configuration: str) -> dict:
    """Return operator facts without changing readiness or submission policy."""
    configuration = validate_configuration_name(configuration)
    check = _Check(configuration)
    check.read_documents()
    check.check_paths()
    check.check_models()
    states = {finding["status"] for finding in check.findings}
    aggregate = (
        "BLOCKED"
        if states & {"MISSING", "BROKEN"}
        else "INCOMPLETE" if "UNKNOWN" in states else "NO_KNOWN_BLOCKERS"
    )
    return {
        "configuration": configuration,
        "scope": "static",
        "status": aggregate,
        "native_execution": "not_run",
        "runtime_root": str(paths.runtime_root()),
        "capabilities": {"native_configuration_diagnostics": True},
        "findings": check.findings,
        "limits": LIMITS,
    }
