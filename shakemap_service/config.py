# -*- coding: utf-8 -*-
"""ShakeMap service configuration settings.

All fields are permanent infrastructure. Later phases may append fields
but existing fields will not change names, types, or semantics.
"""
import os
from dataclasses import dataclass


@dataclass
class Settings:
    runtime_root: str = os.getenv("RUNTIME_ROOT", "/home/sysop/runtime")
    service_root: str = os.getenv("SERVICE_ROOT", "/home/sysop/runtime/shakemap")
    shakemap_port: int = int(os.getenv("SHAKEMAP_PORT", "9010"))
    shakemap_modules: str = os.getenv(
        "SHAKEMAP_MODULES",
        "select assemble model contour mapping stations gridxml",
    )
    require_mount: str = os.getenv("SHAKEMAP_REQUIRE_MOUNT", "0")

settings = Settings()
