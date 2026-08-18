#!/usr/bin/env bash
# Container-internal identity consistency verification.
set -uo pipefail

MODE="image"
if [[ $# -eq 1 && ( "$1" == "--help" || "$1" == "-h" ) ]]; then
    echo "Usage: $0 [--deployment]"
    echo "Verify immutable image contents or a deployment with mounted data."
    exit 0
elif [[ $# -eq 1 && "$1" == "--deployment" ]]; then
    MODE="deployment"
elif [[ $# -gt 0 ]]; then
    echo "ERROR: unknown verifier option" >&2
    exit 2
fi

PASS=0
FAIL=0
check() {
    if "$@"; then PASS=$((PASS + 1)); echo "PASS: $*";
    else FAIL=$((FAIL + 1)); echo "FAIL: $*" >&2; fi
}

check test "$(id -u)" = 1000
check test "$(id -g)" = 1000
check command -v shake
check command -v sm_profile
check command -v shake-in-docker
check bash -n /app/entrypoint.sh
check sm_profile --help
check shake-in-docker --help
check python -m pip check

IDENTITY_RESULT="$(python - <<'PY'
import hashlib
import importlib.metadata
import json
import pathlib
import subprocess

from shakemap_service.build_identity import (
    BUILD_IDENTITY_SCHEMA_VERSION,
    validate_build_identity,
)
from shakemap_service.release import load_declared_release_tag

errors = []
path = pathlib.Path('/opt/shakemap-build/identity.json')
try:
    manifest = validate_build_identity(json.loads(path.read_text()))
    image = manifest['immutable_image']
    if manifest['schema_version'] != BUILD_IDENTITY_SCHEMA_VERSION:
        errors.append(
            f"identity schema is not {BUILD_IDENTITY_SCHEMA_VERSION}"
        )
    head = subprocess.run(['git', '-C', '/opt/shakemap', 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    if head != image['upstream']['source_commit']:
        errors.append('source commit mismatch')
    if importlib.metadata.version('shakemap') != image['installed']['shakemap_distribution_version']:
        errors.append('ShakeMap version mismatch')
    if importlib.metadata.version('shakemap-modules') != image['installed']['shakemap_modules_distribution_version']:
        errors.append('module version mismatch')
    declared = load_declared_release_tag(pathlib.Path('/opt/shakemap-build/VERSIONS.env'))
    if image['upstream']['release_tag'] != declared:
        errors.append('installed release differs from VERSIONS.env')
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
    slab2 = support['slab2']
    slab_manifest_path = pathlib.Path(slab2['source_manifest_path'])
    slab_inventory_path = pathlib.Path(slab2['installed_files_manifest_path'])
    slab_archive_path = pathlib.Path(slab2['source_archive_path'])
    slab_root = pathlib.Path(slab2['slabs_dir'])
    if hashlib.sha256(slab_manifest_path.read_bytes()).hexdigest() != slab2['source_manifest_sha256']:
        errors.append('Slab2 source manifest digest mismatch')
    if hashlib.sha256(slab_inventory_path.read_bytes()).hexdigest() != slab2['installed_files_manifest_sha256']:
        errors.append('Slab2 installed-file manifest digest mismatch')
    if hashlib.sha256(slab_archive_path.read_bytes()).hexdigest() != slab2['source_archive_sha256']:
        errors.append('Slab2 source archive digest mismatch')
    inventory = json.loads(slab_inventory_path.read_text())
    if len(inventory.get('files', [])) != slab2['file_count']:
        errors.append('Slab2 installed-file count mismatch')
    for record in inventory.get('files', []):
        item = slab_root / record['path']
        if not item.is_file() or item.stat().st_size != record['size'] or hashlib.sha256(item.read_bytes()).hexdigest() != record['sha256']:
            errors.append(f'Slab2 file mismatch: {item}')
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

SEED_RESULT="$(python - <<'PY'
import pathlib

root = pathlib.Path('/opt/shakemap-seeds/regional')
required = {'gmpe_sets.conf', 'model.conf', 'modules.conf', 'products.conf', 'select.conf'}
folders = [path for path in root.iterdir() if path.is_dir() and not path.name.startswith('.')]
print('OK' if folders and all(required <= {item.name for item in folder.iterdir()} for folder in folders) else 'INVALID')
PY
)"
check test "${SEED_RESULT}" = OK

MODULE_RESULT="$(python - <<'PY'
from shakemap_modules.coremods.sm_select import SelectModule
from shakemap_modules.coremods.assemble import AssembleModule
from shakemap_modules.coremods.model import ModelModule
from shakemap_modules.coremods.contour import ContourModule
from shakemap_modules.coremods.mapping import MappingModule
from shakemap_modules.coremods.stations import StationModule
from shakemap_modules.coremods.gridxml import GridXMLModule
from shakemap_service.main import app
from shakemap_service.preparation import inspect_data_assets
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

REQUEST_RESULT="$(python - <<'PY'
import hashlib
import json
import pathlib
import stat

errors = []
root = pathlib.Path('/opt/shakemap-verification')
manifest_path = root / 'scenario-manifest.json'
identity_path = pathlib.Path('/opt/shakemap-build/identity.json')
try:
    if not stat.S_ISREG(manifest_path.lstat().st_mode) or manifest_path.is_symlink():
        raise ValueError('scenario manifest is not a safe regular file')
    manifest = json.loads(manifest_path.read_text())
    identity = json.loads(identity_path.read_text())['immutable_image']
    if manifest.get('schema_version') != 1:
        errors.append('scenario manifest schema mismatch')
    if manifest.get('scenario_id') != 'south-napa-global':
        errors.append('scenario identity mismatch')
    if manifest.get('intended_configuration') != 'global':
        errors.append('scenario configuration mismatch')
    if manifest.get('embedded_fixture_identity') != 'SCENARIO':
        errors.append('embedded fixture identity mismatch')
    compatibility = manifest.get('compatible_shakemap')
    expected_compatibility = {
        'release_tag': identity['upstream']['release_tag'],
        'source_commit': identity['upstream']['source_commit'],
    }
    if compatibility != expected_compatibility:
        errors.append('scenario compatibility mismatch')
    records = manifest.get('files')
    if not isinstance(records, list) or len(records) != 2:
        errors.append('scenario inventory must contain exactly two files')
        records = []
    inventory = {
        record.get('installed_name'): record
        for record in records
        if isinstance(record, dict)
    }
    if set(inventory) != {'event.xml', 'event_dat.xml'}:
        errors.append('scenario inventory names mismatch')
    for name in ('event.xml', 'event_dat.xml'):
        record = inventory.get(name)
        if not isinstance(record, dict):
            continue
        target = root / name
        if target.parent != root or not stat.S_ISREG(target.lstat().st_mode) or target.is_symlink():
            errors.append(f'scenario file is missing or unsafe: {name}')
            continue
        size = target.stat().st_size
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if size != record.get('installed_size') or digest != record.get('installed_sha256'):
            errors.append(f'scenario file identity mismatch: {name}')
except Exception as exc:
    errors.append(f'{type(exc).__name__}: {exc}')
print('OK' if not errors else ' | '.join(errors))
PY
)"
check test "${REQUEST_RESULT}" = OK
if [[ "${REQUEST_RESULT}" != "OK" ]]; then echo "${REQUEST_RESULT}" >&2; fi

check test -x /app/scripts/verify-shakemap-image.sh
check test "$(find /app/scripts -maxdepth 1 -type f | wc -l | tr -d ' ')" = 1
check test ! -e /opt/shakemap-support/global/vs30/global_vs30.grd
check test ! -e /opt/shakemap-support/global/topo/topo_30sec.grd
if [[ "${MODE}" == "image" ]]; then
    check test ! -e /home/sysop/runtime/shakemap/data/global/vs30/global_vs30.grd
    check test ! -e /home/sysop/runtime/shakemap/data/global/topo/topo_30sec.grd
else
    DEPLOYMENT_DATA_RESULT="$(python - <<'PY'
import hashlib
import importlib.metadata
import json
import pathlib

from shakemap_service.preparation import validate_pinned_global_assets

errors = []
data_root = pathlib.Path('/home/sysop/runtime/shakemap/data')
global_result = validate_pinned_global_assets(data_root)
if not global_result['pinned_integrity_valid']:
    for name, result in global_result['global_assets'].items():
        if not result['valid']:
            errors.append(f"global {name}: {result['reason']} at {result['path']}")

try:
    identity = json.loads(pathlib.Path('/opt/shakemap-build/identity.json').read_text())['immutable_image']
    definition = json.loads(pathlib.Path('/opt/shakemap-verification/source-manifest.json').read_text())
    version = identity['installed']['shakemap_distribution_version']
    package_root = data_root / 'test' / version
    manifest = json.loads((package_root / 'package-manifest.json').read_text())
    compatibility = definition['compatibility']
    expected_compatibility = {
        'shakemap_release_tag': identity['upstream']['release_tag'],
        'shakemap_version': version,
        'shakemap_source_commit': identity['upstream']['source_commit'],
        'shakemap_modules_version': identity['installed']['shakemap_modules_distribution_version'],
        'usgs_strec_version': importlib.metadata.version('usgs-strec'),
    }
    if compatibility != expected_compatibility:
        errors.append('verification definition does not match the installed release')
    for field in ('package_id', 'compatibility', 'module_plan', 'image_dependencies'):
        if manifest.get(field) != definition.get(field):
            errors.append(f'verification package {field} mismatch')
    if not (package_root / 'README.md').is_file():
        errors.append('verification package README.md is missing')
    expected_files = {
        item['target_path']: item
        for source in definition['sources']
        for item in source['files']
    }
    recorded_files = {
        item.get('installed_path'): item
        for item in manifest.get('files', [])
        if isinstance(item, dict)
    }
    if set(recorded_files) != set(expected_files):
        errors.append('verification package file inventory mismatch')
    resolved_root = package_root.resolve()
    for relative, expected in expected_files.items():
        target = package_root / relative
        resolved = target.resolve()
        if resolved_root not in resolved.parents or target.is_symlink() or not target.is_file():
            errors.append(f'verification file is missing or unsafe: {relative}')
            continue
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if target.stat().st_size != expected['size'] or digest != expected['sha256']:
            errors.append(f'verification file identity mismatch: {relative}')
        record = recorded_files.get(relative, {})
        if record.get('installed_size') != expected['size'] or record.get('installed_sha256') != expected['sha256']:
            errors.append(f'verification manifest record mismatch: {relative}')
except Exception as exc:
    errors.append(f'{type(exc).__name__}: {exc}')

print('OK' if not errors else ' | '.join(errors))
PY
)"
    check test "${DEPLOYMENT_DATA_RESULT}" = OK
    if [[ "${DEPLOYMENT_DATA_RESULT}" != "OK" ]]; then echo "${DEPLOYMENT_DATA_RESULT}" >&2; fi
fi

echo "Container-internal image verification: ${PASS} passed, ${FAIL} failed"
echo "This result does not establish running-service deployment readiness."
test "${FAIL}" -eq 0
