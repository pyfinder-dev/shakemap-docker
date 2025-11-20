# -*- coding: utf-8 -*-
from pathlib import Path
from .config import settings


def data_root() -> Path:
    return Path(settings.shakemap_data_root)


def profile_dir() -> Path:
    return data_root() / "profiles" / settings.shakemap_profile


def projects_root() -> Path:
    return profile_dir() / "projects"


def event_project_dir(event_id: str) -> Path:
    return projects_root() / event_id
