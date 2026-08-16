# -*- coding: utf-8 -*-
"""Materialize the selected private ShakeMap profile for one calculation."""
from __future__ import annotations

import hashlib
import importlib.resources as resources
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import paths, status
from .native_context import NativeCalculationContext


PROFILE_NAME = "calculation"
BASE_CONFIGURATION_FILES = (
    "modules.conf",
    "gmpe_sets.conf",
    "model.conf",
    "select.conf",
    "products.conf",
    "shake.conf",
    "logging.conf",
    "transfer.conf",
    "migrate.conf",
)
REGIONAL_CONFIGURATION_FILES = (
    "gmpe_sets.conf",
    "model.conf",
    "modules.conf",
    "products.conf",
    "select.conf",
)
STREC_DATA_DIRECTORY = "/opt/shakemap-support/strec"
STREC_SLAB_DIRECTORY = "/opt/shakemap-support/slab2/slabs"


@dataclass(frozen=True)
class HelperExecution:
    command: tuple[str, ...]
    output: str
    return_code: int


@dataclass(frozen=True)
class NativeProfileMaterialization:
    selected_configuration: str
    profile_name: str
    source_directory: Optional[Path]
    profile_directory: Path
    home_directory: Path
    install_directory: Path
    data_directory: Path
    selector_file: Path
    configuration_directory: Path
    mapping_directory: Path
    strec_configuration_file: Path
    profile_helper: HelperExecution
    strec_helper: HelperExecution


class NativeProfileError(RuntimeError):
    def __init__(
        self,
        selected_configuration: str,
        stage: str,
        message: str,
        *,
        command: Optional[tuple[str, ...]] = None,
        helper: Optional[HelperExecution] = None,
    ) -> None:
        self.selected_configuration = selected_configuration
        self.stage = stage
        self.command = helper.command if helper is not None else command
        self.helper = helper
        super().__init__(
            f"configuration {selected_configuration!r} failed during {stage}: {message}"
        )


def _current_configuration(context: NativeCalculationContext) -> str:
    current = status.read_current_record(context.event_id)
    if current is None:
        raise NativeProfileError(
            "<unavailable>",
            "current_record",
            f"current calculation record for {context.event_id!r} does not exist",
        )
    selected = current.configuration["selected"]

    expected_profile = paths.event_profile_dir(context.event_id)
    expected_home = expected_profile / "home"
    expected_install = expected_profile / "install"
    expected_data = paths.products_dir()
    if (
        context.profile_directory != expected_profile
        or context.home_directory != expected_home
        or context.install_directory != expected_install
        or context.data_directory != expected_data
    ):
        raise NativeProfileError(
            selected,
            "current_record",
            "native context paths do not match the current calculation",
        )
    if (
        current.event_id != context.event_id
        or current.internal_sequence != context.internal_sequence
    ):
        raise NativeProfileError(
            selected,
            "current_record",
            "current calculation record identity does not match the native context",
        )
    if current.status != status.LifecycleState.RUNNING.value:
        raise NativeProfileError(
            selected,
            "current_record",
            "current calculation record must be RUNNING",
        )
    return selected


def _require_unused_native_outputs(
    context: NativeCalculationContext,
    selected: str,
) -> None:
    for output in (
        context.install_directory,
        context.home_directory / ".shakemap",
        context.home_directory / ".strec",
    ):
        if os.path.lexists(output):
            raise NativeProfileError(
                selected,
                "profile_precondition",
                f"native profile output already exists: {output}",
            )


def _regional_source_directory(selected: str) -> Optional[Path]:
    if selected == "global":
        return None
    source_directory = paths.regional_data_dir() / selected
    for filename in REGIONAL_CONFIGURATION_FILES:
        source = source_directory / filename
        if not source.is_file() or not os.access(source, os.R_OK):
            raise NativeProfileError(
                selected,
                "regional_sources",
                f"required readable regional file is missing: {source}",
            )
    return source_directory


def _run_helper(
    command: tuple[str, ...],
    context: NativeCalculationContext,
    selected: str,
    stage: str,
    *,
    helper_input: Optional[str] = None,
) -> HelperExecution:
    arguments: dict[str, object] = {
        "env": context.environment,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "check": False,
        "shell": False,
    }
    if helper_input is not None:
        arguments["input"] = helper_input
    try:
        completed = subprocess.run(command, **arguments)
    except OSError as exc:
        raise NativeProfileError(
            selected,
            f"{stage}_spawn",
            str(exc),
            command=command,
        ) from exc
    evidence = HelperExecution(
        command=command,
        output=completed.stdout or "",
        return_code=completed.returncode,
    )
    if completed.returncode != 0:
        raise NativeProfileError(
            selected,
            stage,
            f"helper exited with code {completed.returncode}",
            helper=evidence,
        )
    return evidence


def _run_profile_helper(
    context: NativeCalculationContext,
    selected: str,
) -> HelperExecution:
    return _run_helper(
        ("sm_profile", "-c", PROFILE_NAME, "-n"),
        context,
        selected,
        "sm_profile",
        helper_input=f"{context.install_directory}\n{context.data_directory}\n",
    )


def _require_profile_structure(
    context: NativeCalculationContext,
    selected: str,
) -> tuple[Path, Path, Path]:
    selector_file = context.home_directory / ".shakemap" / "profiles.conf"
    try:
        selector_text = selector_file.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise NativeProfileError(selected, "profile_structure", str(exc)) from exc

    selector_values: list[str] = []
    calculation_sections = 0
    in_calculation = False
    calculation_values: dict[str, list[str]] = {
        "install_path": [],
        "data_path": [],
    }
    profiles_section_present = False
    for raw_line in selector_text.splitlines():
        line = raw_line.strip()
        if line == "[profiles]":
            profiles_section_present = True
        is_subsection = line.startswith("[[") and line[-2:] == "]]"
        if is_subsection:
            in_calculation = line == f"[[{PROFILE_NAME}]]"
            if in_calculation:
                calculation_sections += 1
            continue
        if "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if name == "profile" and not in_calculation:
            selector_values.append(value)
        if in_calculation and name in calculation_values:
            calculation_values[name].append(value)

    expected_values = {
        "install_path": str(context.install_directory),
        "data_path": str(context.data_directory),
    }
    if (
        selector_values != [PROFILE_NAME]
        or not profiles_section_present
        or calculation_sections != 1
        or any(
            calculation_values[name] != [value]
            for name, value in expected_values.items()
        )
    ):
        raise NativeProfileError(
            selected,
            "profile_structure",
            "native profile selector or install/data paths are invalid",
        )

    configuration_directory = context.install_directory / "config"
    for filename in BASE_CONFIGURATION_FILES:
        path = configuration_directory / filename
        if not path.is_file() or not os.access(path, os.R_OK):
            raise NativeProfileError(
                selected,
                "profile_structure",
                f"required readable base configuration is missing: {path}",
            )
    mapping_directory = context.install_directory / "data" / "mapping"
    if not mapping_directory.is_dir():
        raise NativeProfileError(
            selected,
            "profile_structure",
            f"native mapping directory is missing: {mapping_directory}",
        )
    if any(mapping_directory.iterdir()):
        raise NativeProfileError(
            selected,
            "profile_structure",
            f"native mapping directory is not empty: {mapping_directory}",
        )
    return selector_file, configuration_directory, mapping_directory


def _copy_mapping_tree(mapping_directory: Path, selected: str) -> None:
    try:
        source_resource = resources.files("shakemap_modules").joinpath(
            "data", "mapping"
        )
        with resources.as_file(source_resource) as source:
            if not source.is_dir():
                raise FileNotFoundError(
                    f"installed mapping directory is missing: {source}"
                )
            shutil.copytree(source, mapping_directory, dirs_exist_ok=True)
    except (OSError, ModuleNotFoundError) as exc:
        raise NativeProfileError(selected, "mapping_copy", str(exc)) from exc


def _replace_single_assignment(
    path: Path,
    name: str,
    replacement: str,
    selected: str,
    stage: str,
) -> None:
    try:
        original = path.read_bytes()
        pattern = re.compile(
            rb"^([ \t]*"
            + re.escape(name.encode("ascii"))
            + rb"[ \t]*=[ \t]*)([^\r\n]*)(\r?\n|$)",
            re.MULTILINE,
        )
        updated, count = pattern.subn(
            lambda match: (
                match.group(1) + replacement.encode("utf-8") + match.group(3)
            ),
            original,
        )
        if count != 1:
            raise ValueError(f"expected exactly one {name} assignment, found {count}")
        path.write_bytes(updated)
    except (OSError, ValueError) as exc:
        raise NativeProfileError(selected, stage, f"{path}: {exc}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _apply_selected_configuration(
    selected: str,
    source_directory: Optional[Path],
    configuration_directory: Path,
) -> None:
    if selected == "global":
        _replace_single_assignment(
            configuration_directory / "model.conf",
            "vs30file",
            str(paths.vs30_grid_path()),
            selected,
            "global_configuration",
        )
        _replace_single_assignment(
            configuration_directory / "products.conf",
            "topography",
            str(paths.topo_grid_path()),
            selected,
            "global_configuration",
        )
        return

    if source_directory is None:
        raise NativeProfileError(
            selected,
            "regional_overlay",
            "source directory is missing",
        )
    for filename in REGIONAL_CONFIGURATION_FILES:
        source = source_directory / filename
        destination = configuration_directory / filename
        try:
            shutil.copyfile(source, destination)
            if source.stat().st_size != destination.stat().st_size:
                raise OSError("destination size differs from selected source")
            if _sha256(source) != _sha256(destination):
                raise OSError("destination hash differs from selected source")
        except OSError as exc:
            raise NativeProfileError(
                selected,
                "regional_overlay",
                f"{filename}: {exc}",
            ) from exc


def _run_strec_helper(
    context: NativeCalculationContext,
    selected: str,
) -> HelperExecution:
    return _run_helper(
        ("strec_cfg", "update", "--datafolder", STREC_DATA_DIRECTORY),
        context,
        selected,
        "strec_cfg",
    )


def materialize_native_profile(
    context: NativeCalculationContext,
) -> NativeProfileMaterialization:
    """Create and retain the current calculation's selected native profile."""
    selected = _current_configuration(context)
    source_directory = _regional_source_directory(selected)
    _require_unused_native_outputs(context, selected)
    profile_helper = _run_profile_helper(context, selected)
    selector_file, configuration_directory, mapping_directory = (
        _require_profile_structure(context, selected)
    )
    _copy_mapping_tree(mapping_directory, selected)
    _apply_selected_configuration(
        selected,
        source_directory,
        configuration_directory,
    )
    strec_helper = _run_strec_helper(context, selected)
    strec_configuration_file = context.home_directory / ".strec" / "config.ini"
    _replace_single_assignment(
        strec_configuration_file,
        "slabfolder",
        STREC_SLAB_DIRECTORY,
        selected,
        "strec_configuration",
    )
    return NativeProfileMaterialization(
        selected_configuration=selected,
        profile_name=PROFILE_NAME,
        source_directory=source_directory,
        profile_directory=context.profile_directory,
        home_directory=context.home_directory,
        install_directory=context.install_directory,
        data_directory=context.data_directory,
        selector_file=selector_file,
        configuration_directory=configuration_directory,
        mapping_directory=mapping_directory,
        strec_configuration_file=strec_configuration_file,
        profile_helper=profile_helper,
        strec_helper=strec_helper,
    )
