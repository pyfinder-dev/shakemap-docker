"""Host-only structure and provenance checks for the tracked request scenario.

These checks do not execute ShakeMap and do not prove native acceptance.
"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from xml.etree import ElementTree


FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shakemap_scenario"
EXPECTED_FILES = {
    "README.md",
    "event.xml",
    "event_dat.xml",
    "request-manifest.json",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ScenarioFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = json.loads(
            (FIXTURE / "request-manifest.json").read_text(encoding="utf-8")
        )

    def test_capability_oriented_file_set(self) -> None:
        self.assertTrue(FIXTURE.is_dir())
        self.assertEqual(
            {path.name for path in FIXTURE.iterdir() if path.is_file()},
            EXPECTED_FILES,
        )
        self.assertFalse((FIXTURE / "rupture.json").exists())
        self.assertFalse((FIXTURE / "moment.xml").exists())
        self.assertFalse((FIXTURE / "requeststatus.json").exists())

    def test_manifest_identity_compatibility_and_license(self) -> None:
        self.assertEqual(self.manifest["schema_version"], 1)
        self.assertEqual(self.manifest["fixture_id"], "SCENARIO")
        self.assertEqual(self.manifest["classification"], "SCENARIO")
        self.assertEqual(
            self.manifest["compatible_shakemap"],
            {
                "release_tag": "v4.4.9",
                "source_commit": "8923f1ff6e82fc866d928a33d1e19e45f276db52",
            },
        )
        source = self.manifest["source"]
        self.assertEqual(
            source["repository"],
            "https://code.usgs.gov/ghsc/esi/shakemap.git",
        )
        self.assertIn("public domain", source["license"].lower())
        self.assertIn("CC0-1.0", source["license"])

    def test_manifest_matches_tracked_bytes(self) -> None:
        entries = {entry["installed_name"]: entry for entry in self.manifest["files"]}
        self.assertEqual(set(entries), {"event.xml", "event_dat.xml"})
        for name, entry in entries.items():
            path = FIXTURE / name
            self.assertEqual(path.stat().st_size, entry["installed_size"])
            self.assertEqual(sha256(path), entry["installed_sha256"])
            self.assertIn(
                "8923f1ff6e82fc866d928a33d1e19e45f276db52",
                entry["source_url"],
            )
            self.assertEqual(len(entry["source_sha256"]), 64)
            self.assertTrue(entry["transformation"])

    def test_event_origin_is_scenario(self) -> None:
        origin = ElementTree.parse(FIXTURE / "event.xml").getroot()
        self.assertEqual(origin.tag, "earthquake")
        self.assertEqual(origin.get("id"), "SCENARIO")
        self.assertEqual(origin.get("event_type"), "SCENARIO")
        self.assertEqual(origin.get("netid"), "nc")
        self.assertEqual(origin.get("time"), "2014-08-24T10:20:44Z")
        self.assertAlmostEqual(float(origin.get("lat", "nan")), 38.2152)
        self.assertAlmostEqual(float(origin.get("lon", "nan")), -122.3123)
        self.assertAlmostEqual(float(origin.get("mag", "nan")), 6.0)
        self.assertAlmostEqual(float(origin.get("depth", "nan")), 11.1)

    def test_observations_are_realistic_and_event_consistent(self) -> None:
        root = ElementTree.parse(FIXTURE / "event_dat.xml").getroot()
        self.assertEqual(root.tag, "shakemap-data")
        embedded_origin = root.find("earthquake")
        station_list = root.find("stationlist")
        self.assertIsNotNone(embedded_origin)
        self.assertIsNotNone(station_list)
        self.assertEqual(embedded_origin.get("id"), "SCENARIO")
        stations = station_list.findall("station")
        components = station_list.findall("station/comp")
        self.assertEqual(len(stations), 334)
        self.assertEqual(len(components), 999)
        self.assertEqual(
            sorted({station.get("netid") for station in stations}),
            ["BG", "BK", "CE", "NC", "NP", "WR"],
        )
        for measure in ("pga", "pgv", "psa03", "psa10", "psa30"):
            values = [
                float(element.get("value", "nan"))
                for component in components
                if (element := component.find(measure)) is not None
            ]
            self.assertGreaterEqual(len(values), 998)
            self.assertTrue(all(value >= 0.0 for value in values))

    def test_readme_states_proof_boundary_and_optional_observations(self) -> None:
        readme = (FIXTURE / "README.md").read_text(encoding="utf-8")
        required_phrases = [
            "SCENARIO",
            "event_dat.xml",
            "v4.4.9",
            "8923f1ff6e82fc866d928a33d1e19e45f276db52",
            "SHA-256",
            "public domain",
            "optional",
            "do not establish native acceptance",
            "does not prove",
        ]
        for phrase in required_phrases:
            self.assertIn(phrase.lower(), readme.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
