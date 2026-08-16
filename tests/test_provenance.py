from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shakemap_service import (
    native_profile,
    paths,
    product_manifest,
    product_validation,
    provenance,
    required_products,
    runner,
    status,
)
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.previous_path_settings = paths.settings
        self.previous_status_settings = status.settings
        configured = Settings(
            runtime_root=self.temporary.name,
            shared_runtime_root="/operator/runtime",
        )
        paths.settings = configured
        status.settings = configured

    def tearDown(self) -> None:
        paths.settings = self.previous_path_settings
        status.settings = self.previous_status_settings
        self.temporary.cleanup()

    def _accept(
        self,
        event_id: str,
        *,
        configuration: str = "global",
        overwrite: bool = True,
    ):
        return accept_request(
            event_id,
            [
                Upload("event.xml", io.BytesIO(f"origin:{event_id}".encode())),
                Upload("event_dat.xml", io.BytesIO(b"observations")),
            ],
            configuration=configuration,
            overwrite=overwrite,
        )

    def _promote(
        self,
        event_id: str,
        *,
        configuration: str = "global",
        overwrite: bool = True,
    ) -> status.CalculationRecord:
        accepted = self._accept(
            event_id,
            configuration=configuration,
            overwrite=overwrite,
        )
        status.transition_to_running(accepted.internal_sequence)
        paths.events_dir().mkdir(parents=True, exist_ok=True)
        paths.queue_entry_dir(accepted.internal_sequence).rename(
            paths.event_service_dir(event_id)
        )
        current = status.read_current_record(event_id)
        if current is None:
            raise AssertionError("promoted test record is missing")
        return current

    @staticmethod
    def _sha256(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    def _write_profile(self, event_id: str) -> dict[str, bytes]:
        profile = paths.event_profile_dir(event_id)
        payloads = {
            "home/.shakemap/profiles.conf": b"profile = calculation\n",
            **{
                f"install/config/{name}": f"configuration:{name}\n".encode()
                for name in native_profile.BASE_CONFIGURATION_FILES
            },
            "home/.strec/config.ini": b"slabfolder = /opt/support/slabs\n",
        }
        for relative, payload in payloads.items():
            path = profile / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
        return payloads

    @staticmethod
    def _build_identity() -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "immutable_image": {
                "available": True,
                "upstream": {
                    "release_tag": "v4.4.9",
                    "source_commit": "a" * 40,
                },
                "support": {
                    "slab2": {"manifest_sha256": "b" * 64},
                    "strec": {"database_sha256": "c" * 64},
                },
            },
            "deployment": {
                "available": True,
                "image_id": "sha256:" + "d" * 64,
                "image_digest": None,
                "invalid_fields": [],
                "source": "runtime_environment",
            },
        }

    @staticmethod
    def _facts(
        *,
        resolution: required_products.RequiredProductResolution | None = None,
    ) -> provenance.ProvenanceFacts:
        return provenance.ProvenanceFacts(
            configuration_materialization={
                "materialized": True,
                "source": "container_global",
                "profile_helper": {"return_code": 0},
            },
            native_execution={
                "command": (
                    "shake",
                    "exact",
                    "select",
                    "assemble",
                    "model",
                    "contour",
                    "mapping",
                    "stations",
                    "gridxml",
                ),
                "pid": 4321,
                "started_at": "2026-08-16T14:00:01.000000Z",
                "completed_at": "2026-08-16T14:00:05.000000Z",
                "exit_code": 0,
                "signal": None,
            },
            required_products=resolution,
            native_outcome={"started": True, "exit_code": 0, "signal": None},
            service_outcome={"completed": True, "successful": True},
            warnings=("native warning", "configuration warning"),
            failure=None,
            timestamps={
                "accepted_at": "2026-08-16T14:00:00.000000Z",
                "started_at": "2026-08-16T14:00:01.000000Z",
                "native_completed_at": "2026-08-16T14:00:05.000000Z",
                "validated_at": "2026-08-16T14:00:06.000000Z",
                "terminal_at": None,
            },
        )

    def test_exact_observed_facts_and_retained_identities_are_published(
        self,
    ) -> None:
        record = self._promote("exact", overwrite=False)
        profile_payloads = self._write_profile("exact")
        resolution = required_products.RequiredProductResolution(
            paths=("shake_result.hdf", "intensity.jpg", "intensity.jpg"),
            source="derived",
        )
        facts = self._facts(resolution=resolution)
        facts_before = copy.deepcopy(facts)
        status_before = paths.event_status_file("exact").read_bytes()
        request_manifest = (
            paths.event_service_dir("exact") / "request-manifest.json"
        )
        request_before = request_manifest.read_bytes()
        environment_before = dict(os.environ)
        profile_before = {
            relative: (paths.event_profile_dir("exact") / relative).read_bytes()
            for relative in profile_payloads
        }
        build_identity = self._build_identity()
        real_open = Path.open
        forbidden_data = Path("/operator/runtime/shakemap/data")
        forbidden_preparation = mock.Mock()
        forbidden_preparation.load_global_assets.side_effect = AssertionError(
            "legacy preparation module invoked"
        )

        def controlled_open(path: Path, *args, **kwargs):
            if path.is_relative_to(forbidden_data):
                raise AssertionError(f"mounted dataset was opened: {path}")
            return real_open(path, *args, **kwargs)

        with (
            mock.patch.dict(
                sys.modules,
                {"shakemap_service.preparation": forbidden_preparation},
            ),
            mock.patch.object(
                provenance.build_identity,
                "service_identity",
                return_value=build_identity,
            ),
            mock.patch.object(Path, "open", new=controlled_open),
            mock.patch.object(
                runner,
                "run_shake",
                side_effect=AssertionError("native execution repeated"),
            ),
            mock.patch.object(
                required_products,
                "resolve_required_products",
                side_effect=AssertionError("product resolution repeated"),
            ),
            mock.patch.object(
                product_validation,
                "validate_required_products",
                side_effect=AssertionError("product validation repeated"),
            ),
            mock.patch.object(
                product_manifest,
                "publish_product_manifest",
                side_effect=AssertionError("product manifest recreated"),
            ),
            mock.patch.object(
                native_profile,
                "materialize_native_profile",
                side_effect=AssertionError("profile materialization repeated"),
            ),
        ):
            self.assertNotIn("preparation", provenance.__dict__)
            provenance_file = provenance.publish_provenance(record, facts)

        forbidden_preparation.load_global_assets.assert_not_called()

        expected_profile_files = [
            {
                "path": relative,
                "identity": {
                    "size": len(profile_payloads[relative]),
                    "sha256": self._sha256(profile_payloads[relative]),
                },
            }
            for relative in provenance._PROFILE_FILES
        ]
        expected_request_files = [
            {
                "basename": "event.xml",
                "size_bytes": len(b"origin:exact"),
                "sha256": self._sha256(b"origin:exact"),
            },
            {
                "basename": "event_dat.xml",
                "size_bytes": len(b"observations"),
                "sha256": self._sha256(b"observations"),
            },
        ]
        shared_service = Path("/operator/runtime/shakemap")
        payload = json.loads(provenance_file.read_text(encoding="utf-8"))
        self.assertEqual(
            payload,
            {
                "event_id": "exact",
                "internal_sequence": record.internal_sequence,
                "request": {
                    "configuration": "global",
                    "overwrite": False,
                    "input_mode": "upload",
                    "files": expected_request_files,
                },
                "configuration": {
                    "selected": "global",
                    "materialization": {
                        "materialized": True,
                        "source": "container_global",
                        "profile_helper": {"return_code": 0},
                    },
                    "profile_files": expected_profile_files,
                },
                "software_identity": build_identity,
                "module_plan": [
                    "select",
                    "assemble",
                    "model",
                    "contour",
                    "mapping",
                    "stations",
                    "gridxml",
                ],
                "native_execution": {
                    "command": [
                        "shake",
                        "exact",
                        "select",
                        "assemble",
                        "model",
                        "contour",
                        "mapping",
                        "stations",
                        "gridxml",
                    ],
                    "pid": 4321,
                    "started_at": "2026-08-16T14:00:01.000000Z",
                    "completed_at": "2026-08-16T14:00:05.000000Z",
                    "exit_code": 0,
                    "signal": None,
                },
                "outcomes": {
                    "native": {"started": True, "exit_code": 0, "signal": None},
                    "service": {"completed": True, "successful": True},
                },
                "warnings": ["native warning", "configuration warning"],
                "failure": None,
                "timestamps": {
                    "accepted_at": "2026-08-16T14:00:00.000000Z",
                    "started_at": "2026-08-16T14:00:01.000000Z",
                    "native_completed_at": "2026-08-16T14:00:05.000000Z",
                    "validated_at": "2026-08-16T14:00:06.000000Z",
                    "terminal_at": None,
                },
                "required_products": {
                    "paths": [
                        "shake_result.hdf",
                        "intensity.jpg",
                        "intensity.jpg",
                    ],
                    "source": "derived",
                },
                "locations": {
                    "input": str(shared_service / "data/inputs/exact"),
                    "native_products": str(
                        shared_service / "products/exact/current/products"
                    ),
                    "status": str(
                        shared_service / ".service/events/exact/status.json"
                    ),
                    "profile": str(
                        shared_service / ".service/events/exact/profile"
                    ),
                    "provenance": str(
                        shared_service / ".service/events/exact/provenance.json"
                    ),
                    "product_manifest": str(
                        shared_service
                        / ".service/events/exact/product-manifest.json"
                    ),
                    "service_log": str(
                        shared_service / ".service/events/exact/logs/service.log"
                    ),
                    "shake_log": str(
                        shared_service / ".service/events/exact/logs/shake.log"
                    ),
                },
                "large_datasets": {
                    "global": {
                        "vs30": {
                            "path": str(
                                shared_service
                                / "data/global/vs30/global_vs30.grd"
                            ),
                            "manifest_identity": {
                                "size": 610189275,
                                "sha256": (
                                    "b07944c5be332c5a261777d23b3390fe8"
                                    "d5638f25b388b82f5dc1e98c6356011"
                                ),
                                "source_url": (
                                    "https://apps.usgs.gov/shakemap_geodata/"
                                    "vs30/global_vs30.grd"
                                ),
                                "checksum_authority": (
                                    "project-verified download pin; USGS "
                                    "publishes no checksum alongside the file"
                                ),
                            },
                            "calculation_validation": "not_observed",
                        },
                        "topography": {
                            "path": str(
                                shared_service
                                / "data/global/topo/topo_30sec.grd"
                            ),
                            "manifest_identity": {
                                "size": 249661705,
                                "sha256": (
                                    "3aa02a77d56d656deae9bf4539afdb3c"
                                    "e1dd1b7057a67a5c7bdd0573fc97bd4c"
                                ),
                                "source_url": (
                                    "https://apps.usgs.gov/shakemap_geodata/"
                                    "topo/topo_30sec.grd"
                                ),
                                "checksum_authority": (
                                    "project-verified download pin; USGS "
                                    "publishes no checksum alongside the file"
                                ),
                            },
                            "calculation_validation": "not_observed",
                        },
                    },
                    "regional": {
                        "path": None,
                        "manifest_identity": None,
                        "identity_state": "not_applicable",
                    },
                },
            },
        )
        self.assertEqual(facts, facts_before)
        self.assertEqual(paths.event_status_file("exact").read_bytes(), status_before)
        self.assertEqual(request_manifest.read_bytes(), request_before)
        self.assertEqual(
            {
                relative: (paths.event_profile_dir("exact") / relative).read_bytes()
                for relative in profile_payloads
            },
            profile_before,
        )
        self.assertEqual(dict(os.environ), environment_before)
        self.assertEqual(stat.S_IMODE(provenance_file.stat().st_mode) & 0o077, 0)

    def test_unavailable_and_regional_identity_evidence_remains_explicit(
        self,
    ) -> None:
        record = self._promote("regional", configuration="romania")
        facts = provenance.ProvenanceFacts(
            configuration_materialization={
                "materialized": False,
                "stage": "regional_sources",
                "failure": "regional source is unavailable",
            },
            native_execution=None,
            required_products=None,
            native_outcome=None,
            service_outcome={"completed": True, "successful": False},
            warnings=(),
            failure={
                "phase": "calculation_preparation",
                "message": "regional source is unavailable",
            },
            timestamps={
                "accepted_at": "2026-08-16T14:00:00.000000Z",
                "terminal_at": None,
            },
        )
        unavailable_identity = {
            "schema_version": "1.0",
            "immutable_image": {
                "available": False,
                "reason": "recorded build identity unavailable",
            },
            "deployment": {
                "available": False,
                "image_id": None,
                "image_digest": None,
                "invalid_fields": [],
                "source": "unavailable",
            },
        }

        with mock.patch.object(
            provenance.build_identity,
            "service_identity",
            return_value=unavailable_identity,
        ):
            provenance_file = provenance.publish_provenance(record, facts)

        payload = json.loads(provenance_file.read_text(encoding="utf-8"))
        self.assertEqual(payload["software_identity"], unavailable_identity)
        self.assertIsNone(payload["native_execution"])
        self.assertIsNone(payload["outcomes"]["native"])
        self.assertIsNone(payload["required_products"])
        self.assertTrue(
            all(
                entry["identity"] is None
                for entry in payload["configuration"]["profile_files"]
            )
        )
        self.assertEqual(
            payload["large_datasets"]["regional"],
            {
                "path": "/operator/runtime/shakemap/data/regional/romania",
                "manifest_identity": None,
                "identity_state": "manifest_identity_unavailable",
            },
        )

    def test_requires_matching_current_running_record_before_reads(self) -> None:
        queued = self._accept("queued")
        queued_record = status.read_status(queued.internal_sequence)
        if queued_record is None:
            raise AssertionError("queued test record is missing")
        facts = self._facts()
        with self.assertRaisesRegex(ValueError, "supplied.*RUNNING"):
            provenance.publish_provenance(queued_record, facts)

        missing = self._accept("missing")
        missing_record = status.transition_to_running(missing.internal_sequence)
        with self.assertRaisesRegex(FileNotFoundError, "does not exist"):
            provenance.publish_provenance(missing_record, facts)

        self._promote("stale")
        later = self._accept("stale")
        later_record = status.transition_to_running(later.internal_sequence)
        with self.assertRaisesRegex(ValueError, "identity does not match"):
            provenance.publish_provenance(later_record, facts)

        supplied_running = self._promote("terminal")
        terminal = status.transition_current_record(
            "terminal",
            status.LifecycleState.FAILED,
            failure={"code": "test_failure", "message": "fixture"},
            service_outcome={"completed": True, "successful": False},
        )
        with self.assertRaisesRegex(ValueError, "current.*RUNNING"):
            provenance.publish_provenance(supplied_running, facts)
        with self.assertRaisesRegex(ValueError, "supplied.*RUNNING"):
            provenance.publish_provenance(terminal, facts)

        for event_id in ("queued", "missing", "stale", "terminal"):
            self.assertFalse(paths.event_provenance_file(event_id).exists())

    def test_request_identity_failure_prevents_publication(self) -> None:
        record = self._promote("request-failure")
        manifest = paths.event_service_dir("request-failure") / "request-manifest.json"
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["event_id"] = "different"
        manifest.write_text(json.dumps(payload), encoding="utf-8")

        with self.assertRaisesRegex(provenance.ProvenanceError, "identity"):
            provenance.publish_provenance(record, self._facts())

        self.assertFalse(paths.event_provenance_file("request-failure").exists())

    def test_publication_is_private_atomic_and_durable(self) -> None:
        record = self._promote("durable")
        facts = self._facts()
        build_identity = self._build_identity()

        with (
            mock.patch.object(
                provenance.build_identity,
                "service_identity",
                return_value=build_identity,
            ),
            mock.patch.object(
                provenance.os,
                "fsync",
                wraps=os.fsync,
            ) as fsync_call,
        ):
            provenance_file = provenance.publish_provenance(record, facts)

        self.assertEqual(fsync_call.call_count, 2)
        self.assertEqual(stat.S_IMODE(provenance_file.stat().st_mode) & 0o077, 0)
        preceding = provenance_file.read_bytes()

        with (
            mock.patch.object(
                provenance.build_identity,
                "service_identity",
                return_value=build_identity,
            ),
            mock.patch.object(
                provenance.os,
                "write",
                side_effect=OSError("injected write failure"),
            ),
        ):
            with self.assertRaisesRegex(OSError, "injected write failure"):
                provenance.publish_provenance(record, facts)

        self.assertEqual(provenance_file.read_bytes(), preceding)
        self.assertEqual(
            list(paths.event_service_dir("durable").glob(".provenance-*.tmp")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
