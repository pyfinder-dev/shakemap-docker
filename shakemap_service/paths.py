# -*- coding: utf-8 -*-
"""Pure path helpers for the mounted ShakeMap service runtime."""
from __future__ import annotations

from pathlib import Path

from .config import settings


QUEUE_ENTRY_WIDTH = 20


def runtime_root() -> Path:
    return Path(settings.runtime_root)


def service_root() -> Path:
    return Path(settings.service_root)


def service_dir() -> Path:
    return service_root() / ".service"


def products_dir() -> Path:
    return service_root() / "products"


def logs_dir() -> Path:
    return service_root() / "logs"


def shakemap_data_dir() -> Path:
    return service_root() / "data"


def events_dir() -> Path:
    return service_dir() / "events"


def archive_dir() -> Path:
    return service_dir() / "archive"


def queue_entry_name(sequence: int) -> str:
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError("queue sequence must be a positive integer")
    return f"{sequence:0{QUEUE_ENTRY_WIDTH}d}"


def parse_queue_entry_name(name: str) -> int:
    if len(name) != QUEUE_ENTRY_WIDTH or not name.isascii() or not name.isdigit():
        raise ValueError(
            f"queue entry name must be exactly {QUEUE_ENTRY_WIDTH} ASCII digits"
        )
    sequence = int(name)
    if sequence < 1:
        raise ValueError("queue sequence must be positive")
    return sequence


def queue_entry_dir(sequence: int) -> Path:
    return events_dir() / queue_entry_name(sequence)


def queue_status_file(sequence: int) -> Path:
    return queue_entry_dir(sequence) / "status.json"


def queue_request_dir(sequence: int) -> Path:
    return queue_entry_dir(sequence) / "request"


def queue_claim_lock_file(sequence: int) -> Path:
    return queue_entry_dir(sequence) / "claim.lock"


def queue_sequence_file() -> Path:
    return events_dir() / ".next-sequence"


def queue_sequence_lock_file() -> Path:
    return events_dir() / ".sequence.lock"


def event_products_dir(event_id: str) -> Path:
    return products_dir() / event_id


def event_request_dir(event_id: str) -> Path:
    return event_products_dir(event_id) / "request"


def event_effective_dir(event_id: str) -> Path:
    return event_products_dir(event_id) / "effective"


def event_profile_dir(event_id: str) -> Path:
    return event_products_dir(event_id) / "profile"


def event_current_dir(event_id: str) -> Path:
    return event_products_dir(event_id) / "current"


def event_native_products_dir(event_id: str) -> Path:
    return event_current_dir(event_id) / "products"


def event_logs_dir(event_id: str) -> Path:
    return event_products_dir(event_id) / "logs"


def event_log_file(event_id: str) -> Path:
    return event_logs_dir(event_id) / "execution.log"


def event_status_file(event_id: str) -> Path:
    return event_products_dir(event_id) / "status.json"


def event_metadata_file(event_id: str) -> Path:
    return event_products_dir(event_id) / "metadata.json"


def event_manifest_file(event_id: str) -> Path:
    return event_products_dir(event_id) / "product-manifest.json"


def event_archive_dir(event_id: str) -> Path:
    """Return the archive namespace only; archival is not implemented."""
    return archive_dir() / event_id


def vs30_dir() -> Path:
    return shakemap_data_dir() / "global" / "vs30"


def topo_dir() -> Path:
    return shakemap_data_dir() / "global" / "topo"


def strec_dir() -> Path:
    return shakemap_data_dir() / "global" / "strec"


def slab_dir() -> Path:
    return strec_dir() / "slabs"


def regional_data_dir() -> Path:
    return shakemap_data_dir() / "regional"


def test_data_dir() -> Path:
    return shakemap_data_dir() / "test"


def vs30_grid_path() -> Path:
    return vs30_dir() / "global_vs30.grd"


def topo_grid_path() -> Path:
    return topo_dir() / "topo_30sec.grd"


def all_service_dirs() -> list[Path]:
    return [
        products_dir(),
        logs_dir(),
        shakemap_data_dir(),
        events_dir(),
        archive_dir(),
    ]
