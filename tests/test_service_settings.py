#!/usr/bin/env python3
"""Host tests for frozen service and deployment settings."""
from __future__ import annotations

import dataclasses
import unittest

from shakemap_service.config import DEFAULT_CONFIGURATION, MODULE_PLAN, Settings


class ServiceSettingsTests(unittest.TestCase):
    def test_code_owned_settings_are_frozen(self) -> None:
        settings = Settings()
        self.assertEqual(
            settings.module_plan,
            ("select", "assemble", "model", "contour", "mapping", "stations", "gridxml"),
        )
        self.assertEqual(settings.module_plan, MODULE_PLAN)
        self.assertEqual(DEFAULT_CONFIGURATION, "global")
        self.assertEqual(settings.required_products, ())
        with self.assertRaises(dataclasses.FrozenInstanceError):
            settings.max_concurrent = 2  # type: ignore[misc]

    def test_service_and_shared_roots_are_derived_separately(self) -> None:
        settings = Settings(
            runtime_root="/container/runtime",
            shared_runtime_root="/operator/runtime",
        )
        self.assertEqual(settings.service_root, "/container/runtime/shakemap")
        self.assertEqual(settings.shared_service_root, "/operator/runtime/shakemap")

    def test_only_approved_environment_settings_are_read(self) -> None:
        settings = Settings.from_environment(
            {
                "RUNTIME_ROOT": "/container/root",
                "SHAKEMAP_SHARED_RUNTIME_ROOT": "/host/root",
                "SHAKEMAP_MAX_CONCURRENT": "12",
                "SERVICE_ROOT": "/forbidden/service",
                "SHAKEMAP_MODULES": "model",
                "SHAKEMAP_REQUIRED_PRODUCTS": "unexpected",
            }
        )
        self.assertEqual(settings.service_root, "/container/root/shakemap")
        self.assertEqual(settings.shared_service_root, "/host/root/shakemap")
        self.assertEqual(settings.max_concurrent, 12)
        self.assertEqual(settings.module_plan, MODULE_PLAN)
        self.assertEqual(settings.required_products, ())

    def test_maximum_concurrency_must_be_positive(self) -> None:
        for value in ("0", "-1", "not-an-integer"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "positive integer"
            ):
                Settings.from_environment({"SHAKEMAP_MAX_CONCURRENT": value})


if __name__ == "__main__":
    unittest.main(verbosity=2)
