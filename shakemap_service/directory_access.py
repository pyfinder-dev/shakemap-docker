# -*- coding: utf-8 -*-
"""Descriptor-backed access to service-owned directory trees."""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from . import paths


@dataclass
class DirectoryHandle:
    path: Path
    descriptor: int

    def close(self) -> None:
        os.close(self.descriptor)


def directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )


def open_service_directory(path: Path, *, create: bool) -> DirectoryHandle:
    runtime_root = Path(paths.settings.runtime_root)
    try:
        relative = path.relative_to(runtime_root)
    except ValueError as exc:
        raise ValueError(
            f"service directory is outside the configured runtime: {path}"
        ) from exc
    if ".." in relative.parts:
        raise ValueError(
            f"service directory is outside the configured runtime: {path}"
        )

    try:
        descriptor = os.open(runtime_root, directory_open_flags())
    except OSError as exc:
        raise ValueError(
            f"configured runtime directory is missing or unsafe: {runtime_root}: {exc}"
        ) from exc
    try:
        # Open one component at a time without following links so every step
        # remains a real directory beneath the configured runtime.
        for component in relative.parts:
            try:
                child = os.open(
                    component,
                    directory_open_flags(),
                    dir_fd=descriptor,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
                # Persist each parent entry before building deeper state so a
                # restart cannot expose only the lower part of the directory chain.
                os.fsync(descriptor)
                try:
                    child = os.open(
                        component,
                        directory_open_flags(),
                        dir_fd=descriptor,
                    )
                except OSError as exc:
                    raise ValueError(
                        f"unsafe service directory ancestry for {path}: "
                        f"{component}: {exc}"
                    ) from exc
            except OSError as exc:
                raise ValueError(
                    f"unsafe service directory ancestry for {path}: {component}: {exc}"
                ) from exc
            os.close(descriptor)
            descriptor = child
        details = os.fstat(descriptor)
        if not stat.S_ISDIR(details.st_mode):
            raise ValueError(f"required service path is not a directory: {path}")
        return DirectoryHandle(
            path=path,
            descriptor=descriptor,
        )
    except BaseException:
        os.close(descriptor)
        raise
