# -*- coding: utf-8 -*-
# shakemap_service configuration settings
import os
from dataclasses import dataclass


@dataclass
class Settings:
    shakemap_data_root: str = os.getenv("SHAKEMAP_DATA_ROOT", "/data/shakemap")
    shakemap_profile: str = os.getenv("SHAKEMAP_PROFILE", "default")
    shakemap_port: int = int(os.getenv("SHAKEMAP_PORT", "9010"))


settings = Settings()
