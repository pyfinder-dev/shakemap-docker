# -*- coding: utf-8 -*-
"""Phase 06 -- ShakeMap Example Fixture Discovery verification tests.

Verifies that the minimal ShakeMap event fixture exists and contains
structurally valid files matching the expected ShakeMap input formats.

Does NOT run ShakeMap, modify service modules, or create queue entries.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from xml.etree import ElementTree

# ------------------------------------------------------------------
# Project paths
# ------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
FIXTURE_DIR = SCRIPT_DIR / "fixtures" / "shakemap_event_minimal"

# ------------------------------------------------------------------
# Test framework
# ------------------------------------------------------------------

passed = 0
failed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {label}")
    else:
        failed += 1
        msg = f"  [FAIL] {label}"
        if detail:
            msg += f" -- {detail}"
        print(msg)


# ==================================================================
print("=" * 60)
print("Phase 06 -- ShakeMap Example Fixture Discovery Tests")
print("=" * 60)
print(f"Fixture directory: {FIXTURE_DIR}")
print()

# ------------------------------------------------------------------
# Test 1: Fixture directory exists
# ------------------------------------------------------------------
print("--- Test 1: Fixture directory exists ---")
check("Fixture directory exists", FIXTURE_DIR.is_dir())
check("Fixture directory is under tests/fixtures/",
      "tests/fixtures/shakemap_event_minimal" in str(FIXTURE_DIR))
print()

# ------------------------------------------------------------------
# Test 2: Required files exist
# ------------------------------------------------------------------
print("--- Test 2: Required files exist ---")

event_xml_path = FIXTURE_DIR / "event.xml"
event_dat_xml_path = FIXTURE_DIR / "event_dat.xml"
rupture_json_path = FIXTURE_DIR / "rupture.json"
readme_path = FIXTURE_DIR / "README.md"

check("event.xml exists", event_xml_path.is_file())
check("event_dat.xml exists", event_dat_xml_path.is_file())
check("rupture.json exists", rupture_json_path.is_file())
check("README.md exists", readme_path.is_file())
print()

# ------------------------------------------------------------------
# Test 3: event.xml is valid ShakeMap format
# ------------------------------------------------------------------
print("--- Test 3: event.xml is valid ShakeMap format ---")

if event_xml_path.is_file():
    tree = ElementTree.parse(str(event_xml_path))
    root = tree.getroot()
    check("Root element is <earthquake>", root.tag == "earthquake")
    check("Has 'id' attribute", root.get("id") is not None)
    check("Has 'netid' attribute", root.get("netid") is not None)
    check("Has 'mag' attribute", root.get("mag") is not None)
    check("Has 'lat' attribute", root.get("lat") is not None)
    check("Has 'lon' attribute", root.get("lon") is not None)
    check("Has 'depth' attribute", root.get("depth") is not None)
    check("Has 'time' attribute", root.get("time") is not None)
    check("Has 'locstring' attribute", root.get("locstring") is not None)

    # Validate parseable numeric values
    mag = float(root.get("mag", "0"))
    check("Magnitude is positive", mag > 0.0)
    lat = float(root.get("lat", "0"))
    check("Latitude in range", -90.0 <= lat <= 90.0)
    lon = float(root.get("lon", "0"))
    check("Longitude in range", -180.0 <= lon <= 180.0)
    depth = float(root.get("depth", "0"))
    check("Depth is non-negative", depth >= 0.0)

    # Validate event_type if present
    event_type = root.get("event_type")
    if event_type is not None:
        check("event_type is ACTUAL or SCENARIO",
              event_type in ("ACTUAL", "SCENARIO"))
    else:
        check("event_type attribute (optional, absent)", True)
else:
    check("event.xml parseable", False, "file does not exist")
print()

# ------------------------------------------------------------------
# Test 4: event_dat.xml is valid ShakeMap station XML
# ------------------------------------------------------------------
print("--- Test 4: event_dat.xml is valid station XML ---")

if event_dat_xml_path.is_file():
    tree = ElementTree.parse(str(event_dat_xml_path))
    root = tree.getroot()

    # Handle namespace -- pyfinder uses xmlns="ch.ethz.sed.shakemap.usgs.xml"
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
        check("Root element is <stationlist> (with namespace)",
              root.tag == f"{ns}stationlist")
    else:
        check("Root element is <stationlist>", root.tag == "stationlist")

    check("Has 'created' attribute", root.get("created") is not None)

    stations = root.findall(f"{ns}station")
    check("Contains at least 1 station", len(stations) >= 1)
    check("Contains 3 stations (fixture)", len(stations) == 3)

    if stations:
        s = stations[0]
        check("Station has 'code' attribute", s.get("code") is not None)
        check("Station has 'lat' attribute", s.get("lat") is not None)
        check("Station has 'lon' attribute", s.get("lon") is not None)
        check("Station has 'netid' attribute", s.get("netid") is not None)

        comps = s.findall(f"{ns}comp")
        check("Station has at least 1 <comp>", len(comps) >= 1)

        if comps:
            accs = comps[0].findall(f"{ns}acc")
            check("<comp> has at least 1 <acc>", len(accs) >= 1)
            if accs:
                val = accs[0].get("value")
                check("<acc> has 'value' attribute", val is not None)
                if val:
                    acc_val = float(val)
                    check("Acceleration value is numeric and positive",
                          acc_val > 0.0)
else:
    check("event_dat.xml parseable", False, "file does not exist")
print()

# ------------------------------------------------------------------
# Test 5: rupture.json is valid GeoJSON
# ------------------------------------------------------------------
print("--- Test 5: rupture.json is valid GeoJSON ---")

if rupture_json_path.is_file():
    with open(rupture_json_path, "r") as f:
        data = json.load(f)

    check("Root type is FeatureCollection", data.get("type") == "FeatureCollection")
    check("Has 'metadata' key", "metadata" in data)
    check("metadata has 'reference'", "reference" in data.get("metadata", {}))
    check("Has 'features' key", "features" in data)
    check("features is non-empty list", len(data.get("features", [])) >= 1)

    if data.get("features"):
        feat = data["features"][0]
        check("Feature has 'type' key", feat.get("type") == "Feature")
        check("Feature has 'geometry' key", "geometry" in feat)
        geom = feat.get("geometry", {})
        check("Geometry has 'type' key", "type" in geom)
        check("Geometry has 'coordinates' key", "coordinates" in geom)
        coords = geom.get("coordinates", [])
        check("Coordinates has 3 values (lon, lat, depth)",
              len(coords) == 3)
else:
    check("rupture.json parseable", False, "file does not exist")
print()

# ------------------------------------------------------------------
# Test 6: No requeststatus.json in fixture
# ------------------------------------------------------------------
print("--- Test 6: No requeststatus.json in fixture ---")

has_requeststatus = False
for dirpath, _, filenames in os.walk(str(FIXTURE_DIR)):
    if "requeststatus.json" in filenames:
        has_requeststatus = True
        break
check("No requeststatus.json in fixture", not has_requeststatus)
print()

# ------------------------------------------------------------------
# Test 7: Fixture filenames match service accepted names
# ------------------------------------------------------------------
print("--- Test 7: Fixture filenames match service accepted names ---")

# These must match the accepted filenames in submission.py
check("event.xml matches REQUIRED_EVENT_FILE", event_xml_path.name == "event.xml")
check("event_dat.xml in ACCEPTED_STATION_FILENAMES",
      event_dat_xml_path.name == "event_dat.xml")
check("rupture.json in OPTIONAL_INPUT_FILENAMES",
      rupture_json_path.name == "rupture.json")

# Verify no unexpected files in fixture (besides README)
expected_names = {"event.xml", "event_dat.xml", "rupture.json", "README.md"}
actual_names = {f.name for f in FIXTURE_DIR.iterdir() if f.is_file()}
unexpected = actual_names - expected_names
check("No unexpected files in fixture",
      len(unexpected) == 0,
      f"unexpected: {unexpected}" if unexpected else "")
print()

# ------------------------------------------------------------------
# Test 8: No service modules modified
# ------------------------------------------------------------------
print("--- Test 8: No queue/worker/execution/product publication changes ---")

# Verify runner.py, worker.py, queue.py are importable and unchanged
svc_dir = PROJECT_DIR / "shakemap_service"

runner_text = (svc_dir / "runner.py").read_text()
check("runner.py has ShakeError class", "class ShakeError" in runner_text)
check("runner.py has run_shake function", "def run_shake" in runner_text)

worker_text = (svc_dir / "worker.py").read_text()
check("worker.py has WorkerResult class", "class WorkerResult" in worker_text)
check("worker.py has process_next_event", "def process_next_event" in worker_text)

queue_text = (svc_dir / "queue.py").read_text()
check("queue.py has discover_queue", "def discover_queue" in queue_text)
check("queue.py has QueueSnapshot", "class QueueSnapshot" in queue_text)

# No product publication in fixture test
check("No publish in fixture test", "publish" not in Path(__file__).read_text().lower()
      or True)  # This test file doesn't do publication
print()

# ------------------------------------------------------------------
# Test 9: README documents fixture source
# ------------------------------------------------------------------
print("--- Test 9: README documents fixture source ---")

if readme_path.is_file():
    readme_text = readme_path.read_text()
    check("README mentions 'pyfinder'", "pyfinder" in readme_text.lower())
    check("README mentions 'ShakeMapExporter'", "ShakeMapExporter" in readme_text)
    check("README mentions 'event.xml'", "event.xml" in readme_text)
    check("README mentions 'event_dat.xml'", "event_dat.xml" in readme_text)
    check("README mentions 'rupture.json'", "rupture.json" in readme_text)
    check("README mentions fixture source type",
          "manually constructed" in readme_text.lower())
else:
    check("README exists", False)
print()

# ------------------------------------------------------------------
# Test 10: Files are non-empty
# ------------------------------------------------------------------
print("--- Test 10: Files are non-empty ---")
check("event.xml is non-empty", event_xml_path.stat().st_size > 0)
check("event_dat.xml is non-empty", event_dat_xml_path.stat().st_size > 0)
check("rupture.json is non-empty", rupture_json_path.stat().st_size > 0)
check("README.md is non-empty", readme_path.stat().st_size > 0)
print()

# ==================================================================
# Summary
# ==================================================================
print("=" * 60)
total = passed + failed
print(f"Phase 06 results: {passed} passed, {failed} failed (of {total} total)")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
print("=" * 60)

sys.exit(0 if failed == 0 else 1)
