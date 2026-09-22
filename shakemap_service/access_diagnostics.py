"""Host recovery commands for confirmed filesystem access failures.

This module only formats diagnostics. Repair policy stays in the existing host
helpers; errno, never exception wording, determines whether repair is relevant.
"""
from __future__ import annotations

import errno
import os
from pathlib import Path
import shlex


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def access_denied(error: BaseException) -> OSError | None:
    """Recover a real access-denied cause through contextual wrappers."""
    seen: set[int] = set()
    while id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, OSError):
            return error if error.errno in (errno.EACCES, errno.EPERM) else None
        cause = error.__cause__ or error.__context__
        if cause is None:
            break
        error = cause
    return None


def host_runtime(runtime: Path) -> Path | None:
    """Map the image mount to the explicit host mount, never its private path."""
    runtime = runtime.absolute()
    if runtime == Path("/home/sysop/runtime"):
        shared = os.environ.get("SHAKEMAP_SHARED_RUNTIME_ROOT")
        if not shared or not Path(shared).is_absolute():
            return None
        return Path(shared)
    return runtime


def host_command(script: str, *arguments: str) -> str:
    helper = PROJECT_ROOT / "scripts" / script
    # The slim image deliberately does not contain operator repair helpers.
    executable = str(helper) if helper.is_file() else f"./scripts/{script}"
    return shlex.join([executable, *arguments])


def data_read_recovery(data_root: Path, target: Path, retry: str) -> str:
    """Suggest read/traverse repair only for the contracted operator data tree."""
    data_root, target = data_root.absolute(), target.absolute()
    try:
        relative = target.relative_to(data_root)
    except ValueError:
        relative = Path(".")
    if (
        data_root.name != "data"
        or data_root.parent.name != "shakemap"
        or not relative.parts
        or relative.parts[0] not in ("global", "regional", "test")
    ):
        return (
            f"Ask the path owner to grant read and directory traversal access at "
            f"{target}.\n{retry}"
        )
    runtime = host_runtime(data_root.parent.parent)
    if runtime is None:
        return (
            "Host runtime mapping is unavailable; identify the host bind mount and "
            f"ask its owner to grant read/traverse access for {relative}.\n{retry}"
        )
    command = host_command(
        "fix-shakemap-permissions.sh", "--runtime-root", str(runtime),
        "--target", relative.as_posix(),
    )
    return (
        "Read access to the asset and traversal access to its directories are required.\n"
        f"From the host project directory run:\n{command}\n"
        f"If your user lacks authority, run manually:\nsudo {command}\n"
        "Keep the asset in place and revalidate after repair; "
        f"access failure does not establish invalid content or absence.\n{retry}"
    )


def finalization_retry(runtime: Path) -> str:
    host = host_runtime(runtime)
    if host is None:
        return "Then rerun the original host finalization command with its original options."
    return (
        "Then rerun:\n"
        + host_command("finalize-shakemap.sh", "--runtime-root", str(host))
        + "\nReapply the original port/concurrency options to this command."
    )


def finalization_recovery(error: BaseException, runtime: Path) -> str | None:
    denied = access_denied(error)
    if denied is None:
        return None
    filename = denied.filename
    retry = finalization_retry(runtime)
    if not filename or not Path(filename).is_absolute():
        return (
            "Access was denied, but the failing absolute path is unavailable. "
            f"Ask the operator to inspect the reported operation.\n{retry}"
        )
    target = Path(filename)
    service = runtime.absolute() / "shakemap"
    writable = any(
        target == service / root or service / root in target.parents
        for root in ("products", "logs", ".service", "data/inputs")
    )
    host = host_runtime(runtime)
    if writable and host is not None:
        command = host_command(
            "repair-shakemap-writable-paths.sh", "--runtime-root", str(host)
        )
        return (
            f"Access denied at {target}. Service-writable state requires read/write/traverse access. "
            "Wait for accepted work to finish and stop all runtime writers; "
            f"from the host project directory run:\n{command}\n"
            f"If your user lacks authority, run manually:\nsudo {command}\n"
            f"Do not run the finalizer with sudo.\n{retry}"
        )
    # Preparation can be creating missing scientific directories or copying
    # seeds. A read-only helper cannot grant those destination write rights.
    return (
        f"Access denied at {target}. This path is outside the service-writable repair scope; "
        "ask its owner to inspect the reported operation and grant the required directory "
        f"traversal and, for creation/import, write access.\n{retry}"
    )
