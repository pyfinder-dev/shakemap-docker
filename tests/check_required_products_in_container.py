"""Exercise required-product resolution with the supported native release."""
from __future__ import annotations

import json
import os
from pathlib import Path

from shakemap_service import (
    native_context,
    native_profile,
    paths,
    recalculation,
    required_products,
    runner,
    status,
)
from shakemap_service.config import Settings
from shakemap_service.submission import Upload, accept_request


CHECK_ROOT = Path("/tmp/m5e-check")
RUNTIME_ROOT = CHECK_ROOT / "runtime"
FIXTURE_FILE = CHECK_ROOT / "fixture" / "event.xml"
MOUNTED_DATA_ROOT = Path("/home/sysop/runtime/shakemap/data")
EVENT_ID = "m5e-required-products-check"
EXPECTED_REQUIRED = (
    "shake_result.hdf",
    "intensity.jpg",
    "pga.jpg",
    "pgv.jpg",
    "psa0p3.jpg",
    "psa1p0.jpg",
    "psa3p0.jpg",
)
EXPECTED_ADDITIONAL_CONVERSIONS = {
    "SA(0.01)": "psa0p01",
    "SA(2.0)": "psa2p0",
    "SA(10.0)": "psa10p0",
}


def _configure_runtime() -> None:
    configured = Settings(
        runtime_root=str(RUNTIME_ROOT),
        shared_runtime_root=str(RUNTIME_ROOT),
    )
    paths.settings = configured
    status.settings = configured
    required_products.settings = configured


def _link_global_data() -> dict[Path, os.stat_result]:
    sources: dict[Path, os.stat_result] = {}
    for relative in (
        Path("global/vs30/global_vs30.grd"),
        Path("global/topo/topo_30sec.grd"),
    ):
        source = MOUNTED_DATA_ROOT / relative
        if not source.is_file() or not os.access(source, os.R_OK):
            raise RuntimeError(f"mounted global source is unavailable: {source}")
        sources[source] = source.stat()
        destination = paths.shakemap_data_dir() / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source)
    return sources


def _prepare_native_products() -> Path:
    with FIXTURE_FILE.open("rb") as fixture:
        accepted = accept_request(
            EVENT_ID,
            [Upload("event.xml", fixture)],
            configuration="global",
        )
    status.transition_to_running(accepted.internal_sequence)
    prepared = recalculation.prepare_calculation(accepted.internal_sequence)
    context = native_context.prepare_native_context(prepared.record, dict(os.environ))
    native_profile.materialize_native_profile(context)
    execution = runner.run_shake(
        EVENT_ID,
        log_file=paths.event_log_file(EVENT_ID),
        env=context.environment,
    )
    if execution.exit_code != 0 or execution.signal is not None:
        raise RuntimeError(
            f"native command failed: exit={execution.exit_code} "
            f"signal={execution.signal}"
        )
    products = paths.event_native_products_dir(EVENT_ID)
    if not products.is_dir() or not products.is_relative_to(paths.products_dir()):
        raise RuntimeError("native products are outside the isolated runtime")
    return products


def _verify_additional_conversions() -> None:
    from esi_shakelib.utils.imt_string import oq_to_file

    actual = {
        imt: oq_to_file(imt)
        for imt in EXPECTED_ADDITIONAL_CONVERSIONS
    }
    if actual != EXPECTED_ADDITIONAL_CONVERSIONS:
        raise RuntimeError(
            f"unexpected installed IMT filename conversions: {actual!r}"
        )


def main() -> int:
    if not CHECK_ROOT.is_dir():
        raise RuntimeError(f"container check root is missing: {CHECK_ROOT}")
    if not FIXTURE_FILE.is_file():
        raise RuntimeError(f"container check fixture is missing: {FIXTURE_FILE}")
    if (RUNTIME_ROOT / "shakemap").exists():
        raise RuntimeError("isolated container-check runtime already exists")

    _configure_runtime()
    mounted_state = _link_global_data()
    products = _prepare_native_products()
    resolution = required_products.resolve_required_products(products)
    if resolution.source != "derived" or resolution.paths != EXPECTED_REQUIRED:
        raise RuntimeError(f"unexpected required-product resolution: {resolution!r}")

    auxiliary_outputs = {
        "mmi_legend.png",
        "pin-thumbnail.png",
        "intensity_overlay.png",
        "intensity.pdf",
    }
    present_auxiliary = sorted(
        name for name in auxiliary_outputs if (products / name).is_file()
    )
    if not present_auxiliary:
        raise RuntimeError("native run produced no auxiliary exclusion evidence")
    if set(resolution.paths) & auxiliary_outputs:
        raise RuntimeError("auxiliary native outputs became required")
    _verify_additional_conversions()

    for source, before in mounted_state.items():
        after = source.stat()
        if (
            after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
            or after.st_mode != before.st_mode
        ):
            raise RuntimeError(f"mounted global source changed during check: {source}")

    print(
        json.dumps(
            {
                "required_products": list(resolution.paths),
                "source": resolution.source,
                "present_auxiliary_outputs": present_auxiliary,
                "additional_imt_conversions": EXPECTED_ADDITIONAL_CONVERSIONS,
                "mounted_data_unchanged": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
