# -*- coding: utf-8 -*-
"""Resolve one immutable official USGS ShakeMap release per image build."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Sequence


OFFICIAL_REPOSITORY_URL = "https://code.usgs.gov/ghsc/esi/shakemap.git"
OFFICIAL_RELEASES_URL = (
    "https://code.usgs.gov/api/v4/projects/ghsc%2Fesi%2Fshakemap/releases"
    "?per_page=100"
)
_STABLE_TAG_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_FULL_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseResolutionError(RuntimeError):
    """Raised when official metadata cannot yield one immutable release."""


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


def select_latest_stable_release(metadata: object) -> str:
    """Select the highest final semver tag from GitLab release metadata.

    Well-formed non-final tags are excluded. Structurally malformed metadata and
    two different tags representing the same semantic version fail closed.
    """
    if not isinstance(metadata, list):
        raise ReleaseResolutionError("Official release metadata is not a JSON list")

    stable: dict[tuple[int, int, int], str] = {}
    for index, item in enumerate(metadata):
        if not isinstance(item, dict):
            raise ReleaseResolutionError(f"Release metadata entry {index} is not an object")
        tag = item.get("tag_name")
        if not isinstance(tag, str) or not tag.strip():
            raise ReleaseResolutionError(
                f"Release metadata entry {index} has no unambiguous tag_name"
            )
        tag = tag.strip()
        if item.get("upcoming_release") is True:
            continue
        try:
            version = stable_version(tag)
        except ReleaseResolutionError:
            # Development, alpha, beta, RC, and unrelated release tags are not
            # candidates. They can coexist with stable official releases.
            continue
        previous = stable.get(version)
        if previous is not None and previous != tag:
            raise ReleaseResolutionError(
                f"Ambiguous official releases for {version}: {previous!r} and {tag!r}"
            )
        stable[version] = tag

    if not stable:
        raise ReleaseResolutionError("Official metadata contains no final stable release")
    return stable[max(stable)]


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


def fetch_release_metadata(url: str = OFFICIAL_RELEASES_URL) -> object:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "shakemap-docker-build/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise ReleaseResolutionError(f"Could not read official release metadata: {exc}") from exc
    try:
        return json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseResolutionError("Official release metadata is not valid JSON") from exc


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


def resolve_latest_official_release() -> ResolvedRelease:
    """Resolve metadata and tag exactly once for this build invocation."""
    tag = select_latest_stable_release(fetch_release_metadata())
    commit = query_official_tag(tag)
    return ResolvedRelease(tag=tag, commit=commit)


def resolve_official_release_tag(tag: str) -> ResolvedRelease:
    """Resolve one requested official stable tag to its exact upstream commit."""
    stable_version(tag)
    return ResolvedRelease(tag=tag, commit=query_official_tag(tag))


def _print_lines(values: Iterable[str]) -> None:
    for value in values:
        if "\n" in value or "\r" in value:
            raise ReleaseResolutionError("Build argument contains a newline")
        print(value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--release-tag")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "resolve":
            if args.release_tag:
                release = resolve_official_release_tag(args.release_tag)
            else:
                release = resolve_latest_official_release()
            _print_lines([release.tag, release.commit, release.repository_url])
            return 0

    except ReleaseResolutionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
