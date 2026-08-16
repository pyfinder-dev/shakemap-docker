"""Resolve the native products required by the service."""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import settings


HDF_PRODUCT = "shake_result.hdf"


class RequiredProductResolutionError(RuntimeError):
    """Required native product paths could not be resolved."""


@dataclass(frozen=True)
class RequiredProductResolution:
    """Record the required relative product paths and their policy source."""

    paths: tuple[str, ...]
    source: Literal["configured", "derived"]


def _validate_relative_path(value: object, source: str) -> str:
    if not isinstance(value, str) or not value:
        raise RequiredProductResolutionError(
            f"{source} required product path must be a nonempty string"
        )
    if value.startswith("/"):
        raise RequiredProductResolutionError(
            f"{source} required product path must be relative: {value!r}"
        )
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise RequiredProductResolutionError(
            f"{source} required product path contains a control character: {value!r}"
        )
    components = value.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise RequiredProductResolutionError(
            f"{source} required product path is not a canonical safe relative path: "
            f"{value!r}"
        )
    return value


def _read_native_imts(hdf_path: Path) -> tuple[str, ...]:
    try:
        import h5py
        from esi_utils_io.smcontainers import ShakeMapOutputContainer
    except Exception as exc:
        raise RequiredProductResolutionError(
            "compatible ShakeMap HDF reader is unavailable"
        ) from exc

    hdf_file = None
    container = None
    primary_message: str | None = None
    primary_cause: Exception | None = None
    raw_imts: tuple[object, ...] = ()
    try:
        try:
            hdf_file = h5py.File(str(hdf_path), "r")
        except Exception as exc:
            primary_message = (
                f"could not open native HDF {hdf_path}: "
                f"{type(exc).__name__}: {exc}"
            )
            primary_cause = exc

        if primary_message is None:
            try:
                container = ShakeMapOutputContainer(hdf_file)
            except Exception as exc:
                primary_message = (
                    f"could not construct native HDF reader for {hdf_path}: "
                    f"{type(exc).__name__}: {exc}"
                )
                primary_cause = exc

        if primary_message is None:
            try:
                raw_imts = tuple(container.getIMTs())
            except Exception as exc:
                primary_message = (
                    f"could not read native IMTs from {hdf_path}: "
                    f"{type(exc).__name__}: {exc}"
                )
                primary_cause = exc
    finally:
        close_message: str | None = None
        close_cause: Exception | None = None
        if container is not None:
            try:
                container.close()
            except Exception as exc:
                close_message = (
                    f"could not close native HDF reader for {hdf_path}: "
                    f"{type(exc).__name__}: {exc}"
                )
                close_cause = exc
        if hdf_file is not None:
            try:
                hdf_file.close()
            except Exception as exc:
                if close_message is None:
                    close_message = (
                        f"could not close native HDF file for {hdf_path}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    close_cause = exc

    if primary_message is not None:
        raise RequiredProductResolutionError(primary_message) from primary_cause
    if close_message is not None:
        raise RequiredProductResolutionError(close_message) from close_cause

    effective_imts: set[str] = set()
    for raw_imt in raw_imts:
        if not isinstance(raw_imt, str):
            raise RequiredProductResolutionError(
                f"native HDF IMT entry is not a string: {raw_imt!r}"
            )
        component, separator, imt = raw_imt.partition("/")
        if (
            not separator
            or not component
            or not imt
            or "/" in imt
            or component != component.strip()
            or imt != imt.strip()
        ):
            raise RequiredProductResolutionError(
                f"native HDF IMT entry is not an unambiguous component/IMT: "
                f"{raw_imt!r}"
            )
        effective_imts.add(imt)

    if not effective_imts:
        raise RequiredProductResolutionError(
            f"native HDF has no effective IMTs: {hdf_path}"
        )
    return tuple(sorted(effective_imts))


def _derive_required_products(products_directory: Path) -> tuple[str, ...]:
    hdf_path = products_directory / HDF_PRODUCT
    effective_imts = _read_native_imts(hdf_path)
    try:
        from esi_shakelib.utils.imt_string import oq_to_file
    except Exception as exc:
        raise RequiredProductResolutionError(
            "compatible ShakeMap IMT filename converter is unavailable"
        ) from exc

    raster_paths: list[str] = []
    seen_paths: set[str] = set()
    for imt in effective_imts:
        if imt == "MMI":
            stem = "intensity"
        else:
            try:
                stem = oq_to_file(imt)
            except Exception as exc:
                raise RequiredProductResolutionError(
                    f"could not map native IMT {imt!r} to a raster filename: "
                    f"{type(exc).__name__}: {exc}"
                ) from exc
        if not isinstance(stem, str) or not stem or "/" in stem:
            raise RequiredProductResolutionError(
                f"native IMT {imt!r} has no unambiguous raster filename"
            )
        raster_path = _validate_relative_path(f"{stem}.jpg", "derived")
        if raster_path in seen_paths:
            raise RequiredProductResolutionError(
                f"native IMTs map ambiguously to {raster_path!r}"
            )
        seen_paths.add(raster_path)
        raster_paths.append(raster_path)
    return (HDF_PRODUCT, *raster_paths)


def resolve_required_products(
    products_directory: Path,
) -> RequiredProductResolution:
    """Resolve code-owned required paths without changing the product tree."""
    configured = settings.required_products
    if configured:
        return RequiredProductResolution(
            paths=tuple(
                _validate_relative_path(value, "configured")
                for value in configured
            ),
            source="configured",
        )

    return RequiredProductResolution(
        paths=_derive_required_products(Path(products_directory)),
        source="derived",
    )
