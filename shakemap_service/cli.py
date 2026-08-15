# -*- coding: utf-8 -*-
"""Disabled host REST-client command foundation."""
from __future__ import annotations

import argparse
import sys
from typing import Sequence


def parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="shake-in-docker",
        description=(
            "Host REST client for shakemap-docker. Public client operations "
            "are not enabled in this implementation."
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser().parse_args(argv)
    print(
        "ERROR: shake-in-docker REST operations are not implemented; "
        "the command cannot submit or inspect calculations yet.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
