# -*- coding: utf-8 -*-
"""Calculation-local filesystem paths and native process environment."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import paths, status


PRIVATE_DIRECTORY_MODE = 0o700
CARTOPY_DATA_DIRECTORY = "/opt/shakemap-support/cartopy"


@dataclass(frozen=True)
class NativeCalculationContext:
    event_id: str
    internal_sequence: int
    profile_directory: Path
    home_directory: Path
    install_directory: Path
    data_directory: Path
    environment: dict[str, str]


def prepare_native_context(
    record: status.CalculationRecord,
    base_environment: Mapping[str, str],
) -> NativeCalculationContext:
    """Create private calculation directories and an isolated environment."""
    if record.status != status.LifecycleState.RUNNING.value:
        raise ValueError("calculation record must be RUNNING")

    current = status.read_current_record(record.event_id)
    if current is None:
        raise FileNotFoundError(
            f"current calculation record for {record.event_id!r} does not exist"
        )
    if (
        current.event_id != record.event_id
        or current.internal_sequence != record.internal_sequence
    ):
        raise ValueError("current calculation record identity does not match")
    if current.status != status.LifecycleState.RUNNING.value:
        raise ValueError("current calculation record must be RUNNING")

    native_current_directory = paths.event_current_dir(record.event_id)
    if not native_current_directory.exists():
        raise FileNotFoundError(
            f"native current directory does not exist: {native_current_directory}"
        )
    if not native_current_directory.is_dir():
        raise NotADirectoryError(
            f"native current path is not a directory: {native_current_directory}"
        )

    profile_directory = paths.event_profile_dir(record.event_id)
    home_directory = profile_directory / "home"
    cache_directory = profile_directory / "cache"
    xdg_cache_directory = cache_directory / "xdg"
    xdg_config_directory = cache_directory / "xdg-config"
    matplotlib_directory = cache_directory / "matplotlib"
    numba_directory = cache_directory / "numba"
    temporary_directory = profile_directory / "tmp"
    install_directory = profile_directory / "install"
    data_directory = paths.products_dir()

    environment = dict(base_environment)
    environment.pop("CALLED_FROM_PYTEST", None)
    environment.pop("CALLED_FROM_MAIN", None)
    environment.update(
        {
            "HOME": str(home_directory),
            "XDG_CACHE_HOME": str(xdg_cache_directory),
            "XDG_CONFIG_HOME": str(xdg_config_directory),
            "MPLCONFIGDIR": str(matplotlib_directory),
            "NUMBA_CACHE_DIR": str(numba_directory),
            "TMPDIR": str(temporary_directory),
            "CARTOPY_DATA_DIR": CARTOPY_DATA_DIRECTORY,
        }
    )

    profile_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    home_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    cache_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    xdg_cache_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    xdg_config_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    matplotlib_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    numba_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)
    temporary_directory.mkdir(mode=PRIVATE_DIRECTORY_MODE)

    return NativeCalculationContext(
        event_id=record.event_id,
        internal_sequence=record.internal_sequence,
        profile_directory=profile_directory,
        home_directory=home_directory,
        install_directory=install_directory,
        data_directory=data_directory,
        environment=environment,
    )
