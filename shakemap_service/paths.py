# -*- coding: utf-8 -*-
"""Path helpers for the ShakeMap service.

All functions return ``pathlib.Path`` objects. Functions are pure path
computation with no side effects — no directories are created, no files
are written.  Exception: ``list_profiles()`` performs a read-only
filesystem scan.

Profile path functions accept an optional ``profile`` parameter,
defaulting to ``settings.shakemap_profile``, enabling future
multi-profile operations without signature changes.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import settings


# ------------------------------------------------------------------
# Contract runtime path functions (§10.2)
# ------------------------------------------------------------------

def runtime_root() -> Path:
    """Return the top-level runtime root."""
    return Path(settings.runtime_root)


def service_root() -> Path:
    """Return the ShakeMap service root."""
    return Path(settings.service_root)


def events_dir() -> Path:
    """Return the durable event tracking directory."""
    return service_root() / "events"


def incoming_dir() -> Path:
    """Return the submitted-inputs staging directory."""
    return service_root() / "incoming"


def work_dir() -> Path:
    """Return the ShakeMap private processing directory."""
    return service_root() / "work"


def products_dir() -> Path:
    """Return the published-outputs directory."""
    return service_root() / "products"


def archive_dir() -> Path:
    """Return the completed-run archive directory."""
    return service_root() / "archive"


def logs_dir() -> Path:
    """Return the service logs directory."""
    return service_root() / "logs"


# ------------------------------------------------------------------
# Per-event path functions (§10.3)
# ------------------------------------------------------------------

def event_events_dir(event_id: str) -> Path:
    """Return the per-event tracking directory."""
    return events_dir() / event_id


def event_service_dir(event_id: str) -> Path:
    """Return the hidden service-internal directory for an event."""
    return events_dir() / event_id / ".shakemap-service"


def event_status_file(event_id: str) -> Path:
    """Return the authoritative requeststatus.json path for an event."""
    return event_service_dir(event_id) / "requeststatus.json"


def event_incoming_dir(event_id: str) -> Path:
    """Return the incoming directory for a specific event."""
    return incoming_dir() / event_id


def event_work_dir(event_id: str) -> Path:
    """Return the work directory for a specific event."""
    return work_dir() / event_id


def event_work_current(event_id: str) -> Path:
    """Return the ShakeMap 'current' sub-directory within work for an event."""
    return work_dir() / event_id / "current"


def event_products_dir(event_id: str) -> Path:
    """Return the products directory for a specific event."""
    return products_dir() / event_id


def event_products_service_dir(event_id: str) -> Path:
    """Return the audit-copy service directory under products for an event."""
    return products_dir() / event_id / ".shakemap-service"


def event_archive_dir(event_id: str) -> Path:
    """Return the archive directory for a specific event."""
    return archive_dir() / event_id


def event_provenance_file(event_id: str) -> Path:
    """Return the provenance.json path for an event.

    Per contract §6.3, provenance MAY be stored in a separate file
    under the hidden service-internal event folder.
    """
    return event_service_dir(event_id) / "provenance.json"


# ------------------------------------------------------------------
# ShakeMap profile path functions (§10.4)
# ------------------------------------------------------------------

def shakemap_home_dir() -> Path:
    """Return the ShakeMap config home (~/.shakemap)."""
    return Path.home() / ".shakemap"


def profiles_conf() -> Path:
    """Return the path to the ShakeMap profiles.conf file."""
    return shakemap_home_dir() / "profiles.conf"


def profile_root(profile: Optional[str] = None) -> Path:
    """Return the root directory for a ShakeMap profile."""
    return Path.home() / "shakemap_profiles" / (profile or settings.shakemap_profile)


def profile_install_dir(profile: Optional[str] = None) -> Path:
    """Return the install directory for a ShakeMap profile."""
    return profile_root(profile) / "install"


def profile_data_dir(profile: Optional[str] = None) -> Path:
    """Return the data directory for a ShakeMap profile."""
    return profile_root(profile) / "data"


def profile_config_dir(profile: Optional[str] = None) -> Path:
    """Return the config directory for a ShakeMap profile."""
    return profile_install_dir(profile) / "config"


def profile_logs_dir(profile: Optional[str] = None) -> Path:
    """Return the ShakeMap logs directory for a profile."""
    return profile_install_dir(profile) / "logs"


# ------------------------------------------------------------------
# Utility functions (§10.5)
# ------------------------------------------------------------------

def all_service_dirs() -> list[Path]:
    """Return all 6 contract service directories."""
    return [
        events_dir(),
        incoming_dir(),
        work_dir(),
        products_dir(),
        archive_dir(),
        logs_dir(),
    ]


def list_profiles() -> list[str]:
    """Return directory names under ~/shakemap_profiles.

    Handles missing profile root gracefully by returning an empty list.
    """
    profile_parent = Path.home() / "shakemap_profiles"
    try:
        return sorted(
            entry.name
            for entry in profile_parent.iterdir()
            if entry.is_dir()
        )
    except FileNotFoundError:
        return []
