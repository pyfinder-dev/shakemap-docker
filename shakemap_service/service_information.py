# -*- coding: utf-8 -*-
"""Read-only construction of public service information."""
from __future__ import annotations

import os
import stat
from typing import Any

from . import paths, readiness as readiness_state
from .build_identity import service_identity
from .config import DEFAULT_CONFIGURATION, Settings, settings
from .public_views import public_identity_projection
from .request_validation import validate_configuration_name


class ServiceInformationError(RuntimeError):
    """Raised when public service information cannot be trusted."""


def read_readiness() -> dict[str, object]:
    """Return the current recorded readiness view."""
    return readiness_state.read_readiness()


def _installed_shakemap_version(identity: object) -> str | None:
    if not isinstance(identity, dict):
        raise ServiceInformationError("service identity is not an object")
    immutable_image = identity.get("immutable_image")
    if not isinstance(immutable_image, dict):
        raise ServiceInformationError("service identity has no immutable image object")
    if immutable_image.get("available") is not True:
        return None
    installed = immutable_image.get("installed")
    if not isinstance(installed, dict):
        raise ServiceInformationError("available service identity has no installed object")
    version = installed.get("shakemap_distribution_version")
    if not isinstance(version, str) or not version.strip():
        raise ServiceInformationError(
            "available service identity has no installed ShakeMap version"
        )
    return version


def _identity_and_version() -> tuple[dict[str, Any], str | None]:
    try:
        identity = service_identity()
        version = _installed_shakemap_version(identity)
    except ServiceInformationError:
        raise
    except Exception as exc:
        raise ServiceInformationError("service identity could not be read") from exc
    return identity, version


def build_health_response() -> dict[str, object]:
    """Build the lightweight public health representation."""
    identity, version = _identity_and_version()
    readiness = readiness_state.read_readiness(identity)
    return {
        "ready": readiness["ready"],
        "reason": readiness["reason"],
        "shakemap_version": version,
    }


def _required_product_policy(selected: Settings) -> dict[str, object]:
    configured = selected.required_products
    return {
        "mode": "configured" if configured else "derived",
        "paths": list(configured),
    }


def build_config_response(selected: Settings | None = None) -> dict[str, object]:
    """Build the effective public operational-configuration representation."""
    effective = settings if selected is None else selected
    identity, version = _identity_and_version()
    try:
        public_identity = public_identity_projection(identity)
    except (TypeError, ValueError) as exc:
        raise ServiceInformationError(
            "service identity could not be projected safely"
        ) from exc
    return {
        "identity": public_identity,
        "shakemap_version": version,
        "module_plan": list(effective.module_plan),
        "default_configuration": DEFAULT_CONFIGURATION,
        "maximum_running": effective.max_concurrent,
        "shared_service_root": effective.shared_service_root,
        "required_products": _required_product_policy(effective),
        "readiness": readiness_state.read_readiness(identity),
    }


def discover_configuration_names() -> list[str]:
    """List safe immediate regional directory names without following links."""
    regional_root = paths.regional_data_dir()
    try:
        root_status = regional_root.lstat()
    except FileNotFoundError:
        return [DEFAULT_CONFIGURATION]
    except OSError as exc:
        raise ServiceInformationError("regional configuration root is unavailable") from exc
    if not stat.S_ISDIR(root_status.st_mode):
        raise ServiceInformationError("regional configuration root is not a real directory")

    discovered: set[str] = set()
    try:
        with os.scandir(regional_root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                discovered.add(validate_configuration_name(entry.name))
    except (OSError, ValueError) as exc:
        raise ServiceInformationError("regional configuration discovery failed") from exc

    discovered.discard(DEFAULT_CONFIGURATION)
    return [DEFAULT_CONFIGURATION, *sorted(discovered)]


def build_configurations_response() -> dict[str, object]:
    """Build the names-only public configuration listing."""
    return {
        "default": DEFAULT_CONFIGURATION,
        "configurations": discover_configuration_names(),
    }
