#!/usr/bin/env python3
"""Install checksum-pinned generic ShakeMap mapping support during image build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(path: Path, record: dict) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing image support file: {path}")
    if path.stat().st_size != record["size"]:
        raise RuntimeError(f"wrong size for image support file: {path}")
    if sha256(path) != record["sha256"]:
        raise RuntimeError(f"wrong SHA-256 for image support file: {path}")


def install(manifest_path: Path, destination: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or len(manifest.get("files", [])) != 20:
        raise RuntimeError("invalid Natural Earth image-support manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".cartopy-install-", dir=destination.parent))
    try:
        for record in manifest["files"]:
            target = temporary / record["target_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            request = urllib.request.Request(
                manifest["url_prefix"] + record["source_path"],
                headers={"User-Agent": "shakemap-docker-image-build/1"},
            )
            with urllib.request.urlopen(request, timeout=120) as response:
                with target.open("wb") as output:
                    shutil.copyfileobj(response, output, length=1024 * 1024)
            verify(target, record)
        if destination.exists():
            raise RuntimeError(f"image support destination already exists: {destination}")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    install(args.manifest, args.destination)


if __name__ == "__main__":
    main()
