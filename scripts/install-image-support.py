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
import zipfile
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


def _download(url: str, destination: Path) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "shakemap-docker-image-build/1"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        with destination.open("wb") as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)


def install_natural_earth(manifest_path: Path, destination: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or len(manifest.get("files", [])) != 20:
        raise RuntimeError("invalid Natural Earth image-support manifest")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".cartopy-install-", dir=destination.parent))
    try:
        for record in manifest["files"]:
            target = temporary / record["target_path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            _download(manifest["url_prefix"] + record["source_path"], target)
            verify(target, record)
        if destination.exists():
            raise RuntimeError(f"image support destination already exists: {destination}")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def install_slab2(manifest_path: Path, destination: Path) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    archive_record = manifest.get("archive", {})
    if (
        manifest.get("schema_version") != 1
        or manifest.get("version") != "Slab2"
        or manifest.get("target_subdirectory") != "slabs"
        or not isinstance(manifest.get("file_count"), int)
        or manifest["file_count"] < 1
    ):
        raise RuntimeError("invalid Slab2 image-support manifest")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".slab2-install-", dir=destination.parent))
    archive = temporary / "source.zip"
    extracted = temporary / manifest["target_subdirectory"]
    inventory = temporary / "installed-files.json"
    try:
        _download(manifest["url"], archive)
        verify(archive, archive_record)
        extracted.mkdir()
        records = []
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) != manifest["file_count"]:
                raise RuntimeError("Slab2 archive file count differs from its manifest")
            for member in members:
                relative = Path(member.filename)
                mode = member.external_attr >> 16
                if (
                    relative.name != member.filename
                    or member.is_dir()
                    or (mode & 0o170000) == 0o120000
                ):
                    raise RuntimeError(f"unsafe Slab2 archive member: {member.filename!r}")
                target = extracted / relative.name
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                records.append(
                    {
                        "path": relative.name,
                        "size": target.stat().st_size,
                        "sha256": sha256(target),
                    }
                )
        inventory.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_manifest": str(manifest_path),
                    "files": records,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            raise RuntimeError(f"image support destination already exists: {destination}")
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="asset", required=True)
    for name in ("natural-earth", "slab2"):
        command = subparsers.add_parser(name)
        command.add_argument("--manifest", type=Path, required=True)
        command.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    if args.asset == "natural-earth":
        install_natural_earth(args.manifest, args.destination)
    else:
        install_slab2(args.manifest, args.destination)


if __name__ == "__main__":
    main()
