#!/usr/bin/env bash
# Container-internal verification of immutable ShakeMap and generic support.
set -uo pipefail

PASS=0
FAIL=0
check() {
    if "$@"; then PASS=$((PASS + 1)); echo "PASS: $*";
    else FAIL=$((FAIL + 1)); echo "FAIL: $*" >&2; fi
}

check test "$(id -u)" = 1000
check test "$(id -g)" = 1000
check command -v shake

IDENTITY_RESULT="$(python - <<'PY'
import hashlib
import importlib.metadata
import json
import pathlib
import subprocess

from shakemap_service.build_identity import validate_build_identity

errors = []
path = pathlib.Path('/opt/shakemap-build/identity.json')
try:
    manifest = validate_build_identity(json.loads(path.read_text()))
    image = manifest['immutable_image']
    if manifest['schema_version'] != 2:
        errors.append('identity schema is not 2')
    head = subprocess.run(['git', '-C', '/opt/shakemap', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    if head != image['upstream']['source_commit']:
        errors.append('source commit mismatch')
    if importlib.metadata.version('shakemap') != image['installed']['shakemap_distribution_version']:
        errors.append('ShakeMap version mismatch')
    if importlib.metadata.version('shakemap-modules') != image['installed']['shakemap_modules_distribution_version']:
        errors.append('module version mismatch')
    support = image['support']
    ne_manifest_path = pathlib.Path(support['natural_earth']['manifest_path'])
    ne_manifest = json.loads(ne_manifest_path.read_text())
    if hashlib.sha256(ne_manifest_path.read_bytes()).hexdigest() != support['natural_earth']['manifest_sha256']:
        errors.append('Natural Earth manifest digest mismatch')
    root = pathlib.Path(support['natural_earth']['cartopy_data_dir'])
    for record in ne_manifest['files']:
        item = root / record['target_path']
        if not item.is_file() or item.stat().st_size != record['size'] or hashlib.sha256(item.read_bytes()).hexdigest() != record['sha256']:
            errors.append(f"Natural Earth file mismatch: {item}")
    strec = support['strec']
    database = pathlib.Path(strec['database_path'])
    link = pathlib.Path(strec['database_link'])
    if not link.is_symlink() or link.resolve() != database.resolve():
        errors.append('STREC support is not a link to the installed database')
    if database.stat().st_size != strec['database_size'] or hashlib.sha256(database.read_bytes()).hexdigest() != strec['database_sha256']:
        errors.append('STREC database identity mismatch')
except Exception as exc:
    errors.append(f'{type(exc).__name__}: {exc}')
print('OK' if not errors else ' | '.join(errors))
PY
)"
check test "${IDENTITY_RESULT}" = OK
if [[ "${IDENTITY_RESULT}" != "OK" ]]; then echo "${IDENTITY_RESULT}" >&2; fi

CARTOPY_RESULT="$(python - <<'PY'
import pathlib
from cartopy.io import shapereader

root = pathlib.Path('/opt/shakemap-support/cartopy').resolve()
requests = [
    ('cultural', 'admin_0_countries'),
    ('cultural', 'admin_1_states_provinces_lines'),
    ('physical', 'lakes'),
    ('physical', 'ocean'),
]
paths = [pathlib.Path(shapereader.natural_earth('10m', category, name)).resolve() for category, name in requests]
print('OK' if all(root in path.parents and path.is_file() for path in paths) else paths)
PY
)"
check test "${CARTOPY_RESULT}" = OK

MODULE_RESULT="$(python - <<'PY'
from shakemap_modules.coremods.sm_select import SelectModule
from shakemap_modules.coremods.assemble import AssembleModule
from shakemap_modules.coremods.model import ModelModule
from shakemap_modules.coremods.contour import ContourModule
from shakemap_modules.coremods.mapping import MappingModule
from shakemap_modules.coremods.stations import StationModule
from shakemap_modules.coremods.gridxml import GridXMLModule
from shakemap_service.main import app
from shakemap_service.preparation import load_preparation
print('OK')
PY
)"
check test "${MODULE_RESULT}" = OK

MAPPING_STACK_RESULT="$(python - <<'PY'
import importlib.metadata
import json

import cartopy
import fiona
import matplotlib
import pyproj
import rasterio
import shapely
from PIL import Image

identity = json.load(open('/opt/shakemap-build/identity.json'))['immutable_image']
compatibility = identity['installed']['mapping_compatibility']
versions = {
    name: importlib.metadata.version(name)
    for name in ('matplotlib', 'cartopy', 'shapely', 'fiona', 'rasterio', 'pyproj', 'pillow')
}
errors = []
if versions['matplotlib'] != compatibility['locked_version']:
    errors.append('matplotlib does not match the resolved release lock')
if compatibility['installed_version'] != versions['matplotlib']:
    errors.append('mapping compatibility record differs from the installed stack')
print('OK' if not errors else ' | '.join(errors))
PY
)"
check test "${MAPPING_STACK_RESULT}" = OK
if [[ "${MAPPING_STACK_RESULT}" != "OK" ]]; then echo "${MAPPING_STACK_RESULT}" >&2; fi

check test -x /app/scripts/verify-shakemap-image.sh
check test "$(find /app/scripts -maxdepth 1 -type f | wc -l | tr -d ' ')" = 1

echo "Container-internal verification: ${PASS} passed, ${FAIL} failed"
test "${FAIL}" -eq 0
