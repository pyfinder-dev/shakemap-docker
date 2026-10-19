"""Normalize and verify the selected ShakeMap environment declarations.

This deliberately supports only the upstream environment's direct requirements;
it is not a Conda solver. Unknown selectors and native dependency declarations
fail for review rather than silently dropping a new upstream requirement.
Micromamba handles the environment solve in a build stage; no manager is needed
by the service runtime.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
from pathlib import Path
from typing import Sequence


POLICY = "resolved_release_environment"
# These are Conda provider names, not Python distribution names. Their
# versions are selected by the upstream environment solve, never by this file.
_NATIVE_PROVIDERS = {"libblas", "libgdal-hdf5", "libgdal-netcdf"}
_ALIASES = {"python-build": "build"}


class UpstreamEnvironmentError(ValueError):
    """The release environment cannot be represented or verified safely."""


def _requirement(declaration: str) -> str:
    from packaging.requirements import InvalidRequirement, Requirement

    # Conda's single '=' and version wildcards map to PEP 440 equality. Build
    # selectors are not generally translatable and must not be discarded.
    translated = re.sub(r"(?<![<>=!~])=(?!=)", "==", declaration)
    name = re.match(r"[A-Za-z0-9_.-]+", translated)
    if name and name.group() in _ALIASES:
        translated = _ALIASES[name.group()] + translated[name.end():]
    try:
        return str(Requirement(translated))
    except InvalidRequirement as exc:
        raise UpstreamEnvironmentError(f"Unsupported upstream requirement: {declaration}") from exc


def _linux_environment(source: Path) -> tuple[Path, str, str, list[str], list[dict]]:
    """Apply known Linux provider adjustments without changing version bounds."""
    path = source / "environment.yml"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UpstreamEnvironmentError(f"Cannot read upstream environment {path}: {exc}") from exc
    lines = []
    excluded = []
    for line in raw.splitlines():
        selector = re.search(r"#\s*\[([^]]+)\]", line)
        if selector:
            if selector.group(1) != "osx":
                raise UpstreamEnvironmentError(f"Unsupported upstream selector: {line.strip()}")
            excluded.append(line.strip())
            continue
        lines.append(line)
    adaptations = []
    if platform.system() == "Linux" and platform.machine().lower() in {"aarch64", "arm64"}:
        # The 4.5 release's esi-core has a PyPI ARM64 distribution but no
        # conda-forge Linux ARM64 package. Move this one declaration intact;
        # do not downgrade it or invent a general package-provider fallback.
        core = [(index, line.strip()[2:]) for index, line in enumerate(lines)
                if re.fullmatch(r"  - esi-core(?:[<>=!~].*)?", line)]
        if core:
            groups = [index for index, line in enumerate(lines) if line == "  - pip:"]
            if len(core) != 1 or len(groups) != 1:
                raise UpstreamEnvironmentError("Review changed upstream esi-core/pip declarations")
            index, requirement = core[0]
            lines[index] = ""
            lines.insert(groups[0] + 1, f"      - {requirement}")
            adaptations.append({
                "platform": "linux-aarch64",
                "requirement": requirement,
                "from": "conda-forge",
                "to": "pip",
                "reason": "esi-core unavailable from conda-forge for Linux ARM64",
            })
    return path, raw, "\n".join(lines) + "\n", excluded, adaptations


def normalize(source: Path, output: Path) -> None:
    """Provide the full upstream environment to the Linux Conda solver."""
    _, _, content, _, _ = _linux_environment(source)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")


def read_environment(source: Path) -> dict:
    """Read the selected release's Linux requirements without version constants."""
    import yaml

    path, raw, content, excluded, adaptations = _linux_environment(source)
    try:
        document = yaml.safe_load(content)
        dependencies = document["dependencies"]
    except (KeyError, TypeError, yaml.YAMLError) as exc:
        raise UpstreamEnvironmentError(f"Cannot read upstream environment {path}: {exc}") from exc
    if not isinstance(dependencies, list):
        raise UpstreamEnvironmentError("Upstream environment dependencies must be a list")
    requirements = []
    native = {}
    python_requirement = None
    for entry in dependencies:
        if isinstance(entry, dict):
            if set(entry) != {"pip"} or not isinstance(entry["pip"], list):
                raise UpstreamEnvironmentError(f"Unsupported upstream dependency group: {entry}")
            requirements.extend(_requirement(value) for value in entry["pip"])
            continue
        if not isinstance(entry, str):
            raise UpstreamEnvironmentError(f"Unsupported upstream dependency: {entry}")
        match = re.match(r"^([A-Za-z0-9_.-]+)(.*)$", entry)
        if not match:
            raise UpstreamEnvironmentError(f"Unsupported upstream dependency: {entry}")
        name, specifier = match.groups()
        if name == "python":
            python_requirement = _requirement(entry)
        elif name in {"proj", "hdf5"}:
            native[name] = _requirement(entry)
        elif name in _NATIVE_PROVIDERS:
            if specifier:
                raise UpstreamEnvironmentError(f"Review newly constrained native provider: {entry}")
            # Native provider declarations are passed unchanged to the solver.
            pass
        elif name == "pip":
            if specifier:
                requirements.append(_requirement(entry))
        else:
            requirements.append(_requirement(entry))
    if not python_requirement or set(native) != {"proj", "hdf5"}:
        raise UpstreamEnvironmentError("Review changed upstream Python/PROJ/HDF5 declarations")
    return {
        "schema_version": 1,
        "policy": POLICY,
        "source_environment_path": str(path),
        "source_environment_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "python_requirement": python_requirement,
        "requirements": sorted(set(requirements)),
        "native_requirements": native,
        "provider_differences": {
            "environment": "Micromamba build-stage native solve of upstream environment.yml",
            "python": "Upstream environment pip dependencies; ShakeMap installed with --no-deps",
            "package_manager_in_runtime": False,
            "conda_inventory_path": "/opt/shakemap-build/conda-packages.txt",
            "resolved_python_inventory_path": "/opt/shakemap-build/upstream-resolved-python.txt",
            "excluded_declarations": excluded,
            "provider_adaptations": adaptations,
            "lock_replication": False,
        },
    }


def _native_versions() -> dict[str, dict[str, str]]:
    import fiona
    import h5py
    import netCDF4
    import pyproj
    import rasterio

    return {
        "proj": {
            "pyproj": pyproj.proj_version_str,
            "rasterio": rasterio.__proj_version__,
            # Fiona exposes this through its compiled extension, not its
            # package-level API. Absence fails the image for explicit review.
            "fiona": ".".join(str(part) for part in fiona._env.get_proj_version_tuple()),
        },
        "hdf5": {
            "h5py": h5py.version.hdf5_version,
            "netCDF4": netCDF4.__hdf5libversion__,
        },
    }


def _crs_checks() -> dict[str, str]:
    """Resolve a real EPSG database entry through every PROJ consumer."""
    import fiona.crs
    import pyproj
    import rasterio.crs

    checks = {}
    for provider, crs_class in (
        ("pyproj", pyproj.CRS),
        ("rasterio", rasterio.crs.CRS),
        ("fiona", fiona.crs.CRS),
    ):
        try:
            crs = crs_class.from_epsg(4326)
            if crs.to_epsg() != 4326:
                raise ValueError("EPSG:4326 lookup returned a different coordinate system")
        except Exception as exc:
            raise UpstreamEnvironmentError(
                f"Installed {provider} cannot resolve EPSG:4326 from its PROJ database: {exc}. "
                "Review the environment's PROJ resources and data search paths."
            ) from exc
        checks[provider] = "EPSG:4326"
    return checks


def verify(source: Path, output: Path | None = None) -> dict:
    """Verify final installed Python and exposed native-library versions."""
    from packaging.requirements import Requirement

    record = read_environment(source)
    installed = {}
    checks = [(record["python_requirement"], "python", platform.python_version())]
    for value in record["requirements"]:
        requirement = Requirement(value)
        try:
            version = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise UpstreamEnvironmentError(f"Missing upstream dependency: {requirement.name}") from exc
        installed[requirement.name] = version
        checks.append((value, requirement.name, version))
    try:
        native = _native_versions()
    except (ImportError, AttributeError) as exc:
        raise UpstreamEnvironmentError(
            f"Cannot inspect installed native-library providers: {exc}. "
            "Review their installed APIs before accepting the image."
        ) from exc
    for name, value in record["native_requirements"].items():
        for provider, version in native[name].items():
            checks.append((value, f"{name} loaded by {provider}", version))
    for value, component, version in checks:
        requirement = Requirement(value)
        if version not in requirement.specifier:
            raise UpstreamEnvironmentError(
                f"Upstream {source / 'environment.yml'} requires {value}; "
                f"{component} has {version}. Review the upstream environment installation."
            )
    record.update({
        "installed_versions": installed,
        "python_version": platform.python_version(),
        "native_versions": native,
        "installation_checks": {"crs_database": _crs_checks()},
        "native_check_scope": (
            "PROJ versions and EPSG:4326 database resolution through pyproj/rasterio/Fiona; "
            "HDF5 versions exposed by h5py/netCDF4"
        ),
    })
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("normalize", "verify", "check"))
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            if args.record is None or args.output is not None:
                parser.error("check requires --record and does not accept --output")
            recorded = json.loads(args.record.read_text(encoding="utf-8"))
            if verify(args.source) != recorded:
                raise UpstreamEnvironmentError("Installed environment differs from its build record")
        else:
            if args.output is None or args.record is not None:
                parser.error("normalize/verify require --output and do not accept --record")
            (normalize if args.command == "normalize" else verify)(args.source, args.output)
    except (UpstreamEnvironmentError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
