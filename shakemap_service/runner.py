# -*- coding: utf-8 -*-
from typing import Sequence
import subprocess


class ShakeError(RuntimeError):
    """Raised when the 'shake' CLI fails."""
    pass


def run_shake(event_id: str, modules: Sequence[str] | None = None, force: bool = False) -> list[str]:
    """
    Build and run the 'shake' command for a given event_id.

    Returns the command list that was executed.
    Raises ShakeError on failure.
    """
    cmd: list[str] = ["shake"]

    if force:
        cmd.append("--force")

    cmd.append(event_id)

    if modules:
        cmd.extend(modules)

    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as exc:
        raise ShakeError(f"'shake' failed with exit code {exc.returncode}") from exc

    return cmd
