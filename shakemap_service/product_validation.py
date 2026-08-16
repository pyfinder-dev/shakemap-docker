"""Validate the generic availability of required native products."""
from __future__ import annotations

import errno
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .required_products import RequiredProductResolution


class ProductValidationInputError(ValueError):
    """Required-product validation input is invalid."""


@dataclass(frozen=True)
class RequiredProductCheck:
    """Record the generic result for one required tuple entry."""

    path: str
    size: int | None
    passed: bool
    reason: str


@dataclass(frozen=True)
class ProductValidationResult:
    """Record ordered generic checks for one required-product resolution."""

    required_paths: tuple[str, ...]
    source: Literal["configured", "derived"]
    checks: tuple[RequiredProductCheck, ...]
    passed: bool


def _validate_input(resolution: RequiredProductResolution) -> None:
    if not isinstance(resolution, RequiredProductResolution):
        raise ProductValidationInputError(
            "resolution must be a RequiredProductResolution"
        )
    if resolution.source not in ("configured", "derived"):
        raise ProductValidationInputError(
            f"required-product source is invalid: {resolution.source!r}"
        )
    if not resolution.paths:
        raise ProductValidationInputError(
            "required-product resolution must not be empty"
        )

    for value in resolution.paths:
        if not isinstance(value, str) or not value:
            raise ProductValidationInputError(
                "required product path must be a nonempty string"
            )
        if value.startswith("/"):
            raise ProductValidationInputError(
                f"required product path must be relative: {value!r}"
            )
        if any(unicodedata.category(character) == "Cc" for character in value):
            raise ProductValidationInputError(
                f"required product path contains a control character: {value!r}"
            )
        components = value.split("/")
        if any(component in {"", ".", ".."} for component in components):
            raise ProductValidationInputError(
                "required product path is not a canonical safe relative path: "
                f"{value!r}"
            )


def _failure(
    path: str,
    size: int | None,
    reason: str,
) -> RequiredProductCheck:
    return RequiredProductCheck(
        path=path,
        size=size,
        passed=False,
        reason=reason,
    )


def _error_text(exc: BaseException) -> str:
    detail = str(exc)
    if detail:
        return f"{type(exc).__name__}: {detail}"
    return type(exc).__name__


def _check_product(
    products_directory: Path,
    resolved_products_directory: Path,
    path: str,
) -> RequiredProductCheck:
    product_path = products_directory / path
    try:
        resolved_product_path = product_path.resolve(strict=True)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return _failure(path, None, "product is missing")
        return _failure(
            path,
            None,
            f"product could not be resolved: {_error_text(exc)}",
        )
    if not resolved_product_path.is_relative_to(resolved_products_directory):
        return _failure(path, None, "product resolves outside products directory")

    try:
        product_stat = product_path.lstat()
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return _failure(path, None, "product is missing")
        return _failure(
            path,
            None,
            f"product could not be inspected: {_error_text(exc)}",
        )

    size = product_stat.st_size
    if not stat.S_ISREG(product_stat.st_mode):
        return _failure(path, size, "product is not a regular file")
    if size == 0:
        return _failure(path, size, "product is empty")

    try:
        with product_path.open("rb"):
            pass
    except OSError as exc:
        return _failure(
            path,
            size,
            f"product is unreadable: {_error_text(exc)}",
        )

    return RequiredProductCheck(
        path=path,
        size=size,
        passed=True,
        reason="generic checks passed",
    )


def validate_required_products(
    products_directory: Path,
    resolution: RequiredProductResolution,
) -> ProductValidationResult:
    """Check required native products without changing their product tree."""
    _validate_input(resolution)
    products_directory = Path(products_directory)
    try:
        resolved_products_directory = products_directory.resolve(strict=True)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            reason = "product is missing"
        else:
            reason = f"products directory could not be resolved: {_error_text(exc)}"
        checks = tuple(
            _failure(path, None, reason)
            for path in resolution.paths
        )
    else:
        checks = tuple(
            _check_product(
                products_directory,
                resolved_products_directory,
                path,
            )
            for path in resolution.paths
        )
    return ProductValidationResult(
        required_paths=resolution.paths,
        source=resolution.source,
        checks=checks,
        passed=all(check.passed for check in checks),
    )
