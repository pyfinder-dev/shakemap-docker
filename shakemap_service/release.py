# -*- coding: utf-8 -*-
"""Resolve one immutable official USGS ShakeMap release per image build."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


OFFICIAL_REPOSITORY_URL = "https://code.usgs.gov/ghsc/esi/shakemap.git"
_STABLE_TAG_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DECLARATION_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=([^\s#]+)$")


class ReleaseResolutionError(RuntimeError):
    """Raised when the declared release cannot be resolved to one immutable source identity."""


@dataclass(frozen=True)
class ResolvedRelease:
    tag: str
    commit: str
    repository_url: str = OFFICIAL_REPOSITORY_URL

    @property
    def version(self) -> str:
        return self.tag[1:] if self.tag.startswith("v") else self.tag


def stable_version(tag: str) -> tuple[int, int, int]:
    """Return a semantic-version tuple for a final release tag only."""
    if not isinstance(tag, str):
        raise ReleaseResolutionError("Release tag is not a string")
    match = _STABLE_TAG_RE.fullmatch(tag)
    if match is None:
        raise ReleaseResolutionError(
            f"Not a final stable ShakeMap release tag: {tag!r}"
        )
    return tuple(int(part) for part in match.groups())


def validate_full_commit(commit: str) -> str:
    """Validate and normalize a full 40-character Git commit."""
    if not isinstance(commit, str) or _FULL_COMMIT_RE.fullmatch(commit.lower()) is None:
        raise ReleaseResolutionError(
            "ShakeMap source commit must be a full 40-character hexadecimal commit"
        )
    return commit.lower()


def resolve_tag_commit_from_ls_remote(tag: str, output: str) -> str:
    """Resolve annotated or lightweight ``git ls-remote`` tag output.

    Annotated tags have both the tag-object ref and a peeled ``^{}`` commit;
    the peeled commit is authoritative. Lightweight tags have only the direct
    ref, which already names the commit.
    """
    stable_version(tag)
    direct_ref = f"refs/tags/{tag}"
    peeled_ref = f"{direct_ref}^{{}}"
    direct: list[str] = []
    peeled: list[str] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) != 2:
            raise ReleaseResolutionError(f"Malformed tag lookup line: {raw_line!r}")
        commit, ref = fields
        commit = validate_full_commit(commit)
        if ref == direct_ref:
            direct.append(commit)
        elif ref == peeled_ref:
            peeled.append(commit)
        else:
            raise ReleaseResolutionError(f"Unexpected tag lookup ref: {ref!r}")

    if len(direct) != 1 or len(peeled) > 1:
        raise ReleaseResolutionError(
            f"Tag {tag!r} did not resolve to one unambiguous official ref"
        )
    return peeled[0] if peeled else direct[0]


def query_official_tag(tag: str, repository_url: str = OFFICIAL_REPOSITORY_URL) -> str:
    stable_version(tag)
    command = [
        "git", "ls-remote", "--tags", repository_url,
        f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}",
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseResolutionError(f"Could not resolve official tag {tag!r}: {exc}") from exc
    return resolve_tag_commit_from_ls_remote(tag, result.stdout)


def resolve_official_release_tag(tag: str) -> ResolvedRelease:
    """Resolve one requested official stable tag to its exact upstream commit."""
    stable_version(tag)
    return ResolvedRelease(tag=tag, commit=query_official_tag(tag))


def load_declared_release_tag(path: Path) -> str:
    """Read the single supported ShakeMap tag from a repository declaration."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleaseResolutionError(f"Could not read release declaration {path}: {exc}") from exc

    declarations: dict[str, str] = {}
    for number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _DECLARATION_RE.fullmatch(line)
        if match is None:
            raise ReleaseResolutionError(
                f"Malformed release declaration at {path}:{number}"
            )
        name, value = match.groups()
        if name in declarations:
            raise ReleaseResolutionError(f"Duplicate release declaration: {name}")
        declarations[name] = value

    tag = declarations.get("SHAKEMAP_RELEASE_TAG")
    if tag is None:
        raise ReleaseResolutionError(
            f"Release declaration {path} has no SHAKEMAP_RELEASE_TAG"
        )
    stable_version(tag)
    return tag


def resolve_declared_release(path: Path) -> ResolvedRelease:
    """Resolve the repository-declared official tag to an immutable commit."""
    return resolve_official_release_tag(load_declared_release_tag(path))


def _print_lines(values: Iterable[str]) -> None:
    for value in values:
        if "\n" in value or "\r" in value:
            raise ReleaseResolutionError("Build argument contains a newline")
        print(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--versions-file", type=Path, required=True)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resolve":
            release = resolve_declared_release(args.versions_file)
            _print_lines([release.tag, release.commit, release.repository_url])
            return 0

    except ReleaseResolutionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
