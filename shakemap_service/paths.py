# -*- coding: utf-8 -*-
"""Path helpers for the ShakeMap service.

All functions return ``pathlib.Path`` objects. Functions are pure path
computation with no side effects — no directories are created, no files
are written.  Exception: ``list_profiles()`` performs a read-only
filesystem scan.

Profile path functions accept an optional ``profile`` parameter,
defaulting to ``settings.shakemap_profile``, enabling future
multi-profile operations without signature changes.

Runtime layout (revised 2026-06-28)::

    runtime/shakemap/               (service_root)
        incoming/                   user-facing: submitted inputs
        products/                   user-facing: published outputs
        logs/                       user-facing: operator logs
        data/                       user-facing: VS30/topo grids
        .service/                   internal: service state
            events/<event_id>/      event tracking + status
            work/<event_id>/        ShakeMap processing scratch
            archive/<event_id>/     completed-run archive
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import settings


# ------------------------------------------------------------------
# Contract runtime path functions
# ------------------------------------------------------------------

def runtime_root() -> Path:
    """Return the top-level runtime root."""
    return Path(settings.runtime_root)


def service_root() -> Path:
    """Return the ShakeMap service root."""
    return Path(settings.service_root)


def service_dir() -> Path:
    """Return the internal service state directory (``.service/``)."""
    return service_root() / ".service"


# -- User-facing top-level directories --

def incoming_dir() -> Path:
    """Return the submitted-inputs staging directory."""
    return service_root() / "incoming"


def products_dir() -> Path:
    """Return the published-outputs directory."""
    return service_root() / "products"


def logs_dir() -> Path:
    """Return the service logs directory."""
    return service_root() / "logs"


# -- Internal directories under .service/ --

def events_dir() -> Path:
    """Return the durable event tracking directory."""
    return service_dir() / "events"


def work_dir() -> Path:
    """Return the ShakeMap private processing directory."""
    return service_dir() / "work"


def archive_dir() -> Path:
    """Return the completed-run archive directory."""
    return service_dir() / "archive"


# ------------------------------------------------------------------
# Per-event path functions
# ------------------------------------------------------------------

def event_events_dir(event_id: str) -> Path:
    """Return the per-event tracking directory under .service/events/."""
    return events_dir() / event_id


def event_status_file(event_id: str) -> Path:
    """Return the authoritative requeststatus.json path for an event.

    Location: ``.service/events/<event_id>/requeststatus.json``
    """
    return events_dir() / event_id / "requeststatus.json"


def event_provenance_file(event_id: str) -> Path:
    """Return the provenance.json path for an event.

    Location: ``.service/events/<event_id>/provenance.json``
    """
    return events_dir() / event_id / "provenance.json"


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


def event_manifest_file(event_id: str) -> Path:
    """Return the products-manifest.json path for an event.

    Location: ``products/<event_id>/products-manifest.json``
    """
    return products_dir() / event_id / "products-manifest.json"


def event_audit_dir(event_id: str) -> Path:
    """Return the audit/service-record directory under published products.

    Location: ``products/<event_id>/service-record/``
    """
    return products_dir() / event_id / "service-record"


def event_log_file(event_id: str) -> Path:
    """Return the per-event execution log path.

    Location: ``logs/<event_id>.log``
    """
    return logs_dir() / f"{event_id}.log"


def event_archive_dir(event_id: str) -> Path:
    """Return the archive directory for a specific event."""
    return archive_dir() / event_id


# ------------------------------------------------------------------
# ShakeMap profile path functions
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


def profile_event_data_dir(event_id: str, profile: Optional[str] = None) -> Path:
    """Return the ShakeMap event data directory inside a profile.

    ShakeMap expects input files at: ``<profile>/data/<event_id>/current/``

    Due to the configure symlink (``profile/data -> SERVICE_ROOT/.service/work``),
    this resolves to ``.service/work/<event_id>/current/`` at container runtime.
    This helper bridges the service layout to ShakeMap's internal
    expectations.
    """
    return profile_data_dir(profile) / event_id / "current"


# ------------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------------

def all_service_dirs() -> list[Path]:
    """Return all directories the service requires at startup.

    Includes the ``.service/`` parent and its subdirectories,
    plus the user-facing top-level directories.
    """
    return [
        # Internal
        service_dir(),
        events_dir(),
        work_dir(),
        archive_dir(),
        # User-facing
        incoming_dir(),
        products_dir(),
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


# ------------------------------------------------------------------
# Stage 2 data and sentinel path functions
# ------------------------------------------------------------------

def shakemap_data_dir() -> Path:
    """Return the shared ShakeMap data directory under SERVICE_ROOT."""
    return service_root() / "data"


def vs30_dir() -> Path:
    """Return the VS30 grid directory."""
    return shakemap_data_dir() / "vs30"


def topo_dir() -> Path:
    """Return the topography grid directory."""
    return shakemap_data_dir() / "topo"


def vs30_grid_path() -> Path:
    """Return the default VS30 grid file path."""
    return vs30_dir() / "global_vs30.grd"


def topo_grid_path() -> Path:
    """Return the default topography grid file path."""
    return topo_dir() / "topo_30sec.grd"


def readiness_sentinel() -> Path:
    """Return the path to the ShakeMap readiness status sentinel file."""
    return shakemap_home_dir() / ".shakemap_readiness_status"
