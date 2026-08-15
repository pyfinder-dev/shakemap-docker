# -*- coding: utf-8 -*-
"""Safety-only validation for caller request identifiers and file names."""
from __future__ import annotations

import os
import unicodedata


NATIVE_BASENAME_LIMIT_BYTES = 255
EVENT_ID_LIMIT_BYTES = 231


def _validate_unchanged_basename(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if value == "":
        raise ValueError(f"{label} cannot be empty")
    if value in {".", ".."}:
        raise ValueError(f"{label} cannot be {value!r}")
    if "/" in value:
        raise ValueError(f"{label} cannot contain a path separator")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise ValueError(f"{label} cannot contain control or surrogate characters")
    try:
        encoded = os.fsencode(value)
    except UnicodeEncodeError as exc:
        raise ValueError(
            f"{label} cannot be represented unchanged by the native filesystem"
        ) from exc
    if len(encoded) > NATIVE_BASENAME_LIMIT_BYTES:
        raise ValueError(
            f"{label} exceeds the native {NATIVE_BASENAME_LIMIT_BYTES}-byte "
            "directory-entry limit"
        )
    return value


def validate_event_id(event_id: object) -> str:
    """Return an event ID only when the native command can preserve it exactly."""
    validated = _validate_unchanged_basename(event_id, "event_id")
    if validated.startswith("-"):
        raise ValueError(
            "event_id cannot begin with '-' because the supported native command "
            "would interpret it as an option"
        )
    encoded_length = len(validated.encode("utf-8"))
    if encoded_length > EVENT_ID_LIMIT_BYTES:
        raise ValueError(
            f"event_id uses {encoded_length} UTF-8 bytes; shorten it to at most "
            f"{EVENT_ID_LIMIT_BYTES} bytes so timestamped archive names fit the "
            f"native {NATIVE_BASENAME_LIMIT_BYTES}-byte directory-entry limit"
        )
    return validated


def validate_configuration_name(configuration: object) -> str:
    """Validate only safe unchanged lookup representation, not native usability."""
    return _validate_unchanged_basename(configuration, "configuration")


def validate_upload_basename(basename: object) -> str:
    """Validate one unchanged top-level native destination basename."""
    return _validate_unchanged_basename(basename, "upload basename")


def validate_overwrite(overwrite: object) -> bool:
    if not isinstance(overwrite, bool):
        raise ValueError("overwrite must be a boolean")
    return overwrite
