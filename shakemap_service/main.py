# -*- coding: utf-8 -*-
from fastapi import FastAPI
from .config import settings

app = FastAPI(title="ShakeMap Service", version="0.1.0")


@app.get("/healthz")
def healthz() -> dict:
    """
    Simple health check endpoint.
    """
    return {
        "status": "ok",
        "shakemap_data_root": settings.shakemap_data_root,
        "shakemap_profile": settings.shakemap_profile,
    }
