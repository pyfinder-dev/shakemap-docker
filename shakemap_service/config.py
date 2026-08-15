# -*- coding: utf-8 -*-
"""Frozen service and deployment settings."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


MODULE_PLAN = (
    "select",
    "assemble",
    "model",
    "contour",
    "mapping",
    "stations",
    "gridxml",
)


def _positive_integer(value: str, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


@dataclass(frozen=True)
class Settings:
    runtime_root: str = "/home/sysop/runtime"
    shared_runtime_root: str = "./runtime"
    shakemap_port: int = 9010
    module_plan: tuple[str, ...] = MODULE_PLAN
    max_concurrent: int = 10
    required_products: tuple[str, ...] = ()

    @property
    def service_root(self) -> str:
        return str(Path(self.runtime_root) / "shakemap")

    @property
    def shared_service_root(self) -> str:
        return str(Path(self.shared_runtime_root) / "shakemap")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> "Settings":
        selected = os.environ if environment is None else environment
        return cls(
            runtime_root=selected.get("RUNTIME_ROOT", "/home/sysop/runtime"),
            shared_runtime_root=selected.get(
                "SHAKEMAP_SHARED_RUNTIME_ROOT", "./runtime"
            ),
            max_concurrent=_positive_integer(
                selected.get("SHAKEMAP_MAX_CONCURRENT", "10"),
                "SHAKEMAP_MAX_CONCURRENT",
            ),
        )


settings = Settings.from_environment()
