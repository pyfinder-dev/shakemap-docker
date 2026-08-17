# -*- coding: utf-8 -*-
"""Public REST views over ShakeMap service identity and durable state."""
from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from python_multipart.exceptions import MultipartParseError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException

from . import (
    paths,
    finalization,
    readiness,
    runner,
    service_information,
    startup_recovery,
    submission,
    worker,
)
from .public_views import (
    DurableStateError,
    UnknownEventError,
    build_current_product_summary,
    build_event_detail,
    build_operational_views,
)
from .service_information import (
    ServiceInformationError,
    build_config_response,
    build_configurations_response,
    build_health_response,
)
from .scheduler import Scheduler


logger = logging.getLogger(__name__)
ADMISSION_INTERVAL_SECONDS = 1.0
SHUTDOWN_GRACE_SECONDS = 60.0
SHUTDOWN_CALLBACK_SECONDS = 2.0
_restart_required_after_incomplete_callback_drain = False


async def _admit_ready_work(scheduler: Scheduler, stopping: asyncio.Event) -> None:
    while not stopping.is_set():
        try:
            def tick_if_ready() -> dict[str, object]:
                with finalization.coordination_lock():
                    state = readiness.read_readiness()
                    if state.get("ready") is True:
                        scheduler.tick()
                    return state

            state = await asyncio.to_thread(tick_if_ready)
            if (
                state.get("ready") is not True
                and state.get("reason") == readiness.UNAVAILABLE
            ):
                logger.error("Calculation admission readiness is unavailable")
        except Exception:
            logger.exception("Calculation admission failed")
        try:
            await asyncio.wait_for(
                stopping.wait(),
                timeout=ADMISSION_INTERVAL_SECONDS,
            )
        except TimeoutError:
            pass


async def _shutdown_scheduler(
    scheduler: Scheduler,
    stopping: asyncio.Event,
    admission_task: asyncio.Task,
) -> None:
    global _restart_required_after_incomplete_callback_drain

    cleanup_error: BaseException | None = None

    def retain_cleanup_error(error: BaseException) -> None:
        nonlocal cleanup_error
        if cleanup_error is None:
            cleanup_error = error

    # Closing the scheduler first lets an in-flight tick finish before stop is visible.
    try:
        scheduler.shutdown(wait=False)
    except BaseException as error:
        retain_cleanup_error(error)
    stopping.set()
    # Admission cancellation is re-raised only after native cleanup completes.
    join_error: BaseException | None = None
    try:
        await admission_task
    except BaseException as error:
        join_error = error

    try:
        await asyncio.to_thread(
            scheduler.wait_until_idle,
            SHUTDOWN_GRACE_SECONDS,
        )
    except BaseException as error:
        retain_cleanup_error(error)

    try:
        runner.close_and_terminate_active()
    except BaseException as error:
        retain_cleanup_error(error)

    try:
        await asyncio.to_thread(
            scheduler.wait_until_idle,
            SHUTDOWN_CALLBACK_SECONDS,
        )
    except BaseException as error:
        retain_cleanup_error(error)

    try:
        runner.force_kill_active()
    except BaseException as error:
        retain_cleanup_error(error)

    try:
        final_idle = await asyncio.to_thread(
            scheduler.wait_until_idle,
            SHUTDOWN_CALLBACK_SECONDS,
        )
    except BaseException as error:
        _restart_required_after_incomplete_callback_drain = True
        retain_cleanup_error(error)
    else:
        if not final_idle:
            _restart_required_after_incomplete_callback_drain = True

    if join_error is not None:
        raise join_error
    if cleanup_error is not None:
        raise cleanup_error


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Recover durable state and own calculation admission while serving."""
    if _restart_required_after_incomplete_callback_drain:
        raise RuntimeError(
            "application process restart is required after an incomplete "
            "calculation callback drain"
        )
    recovered = startup_recovery.recover_interrupted_calculations()
    if recovered:
        logger.info("Recovered %d interrupted calculations", len(recovered))
    finalization.consume_bootstrap_marker()
    runner.open_launch_gate()
    scheduler = Scheduler(worker.execute_shakemap)
    stopping = asyncio.Event()
    admission_task = asyncio.create_task(_admit_ready_work(scheduler, stopping))
    try:
        yield
    finally:
        await _shutdown_scheduler(scheduler, stopping, admission_task)


app = FastAPI(title="ShakeMap Service", version="0.1.0", lifespan=lifespan)


SUBMISSION_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["event_id"],
                    "properties": {
                        "event_id": {"type": "string"},
                        "configuration": {
                            "type": "string",
                            "default": "global",
                        },
                        "overwrite": {
                            "type": "string",
                            "enum": ["true", "false"],
                            "default": "true",
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "string", "format": "binary"},
                        },
                    },
                }
            }
        },
    }
}


def _service_unavailable_response(reason: str) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "service_unavailable",
            "message": reason,
            "details": [],
        },
    )


def _service_information_error_response(error: ServiceInformationError) -> JSONResponse:
    logger.error("Service information unavailable: %s", error)
    return JSONResponse(
        status_code=500,
        content={
            "error": "service_failure",
            "message": "Service information is unavailable",
            "details": [],
        },
    )


def _durable_state_error_response(error: DurableStateError) -> JSONResponse:
    for problem in error.problems:
        logger.error(
            "Malformed durable %s record %s: %s",
            problem.source,
            problem.entry,
            problem.message,
        )
    return JSONResponse(
        status_code=500,
        content={
            "error": "service_failure",
            "message": "Durable calculation state is malformed",
            "details": [
                {
                    "source": problem.source,
                    "entry": problem.entry,
                    "message": "record is malformed or unsafe",
                }
                for problem in error.problems
            ],
        },
    )


def _unknown_event_response(event_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "error": "request_rejected",
            "message": f"Unknown event_id: {event_id}",
            "details": [],
        },
    )


def _request_rejected_response(error: Exception) -> JSONResponse:
    logger.warning("Submission request rejected: %s", error)
    if isinstance(error, submission.InputPublicationError):
        message = "Caller uploads could not be published safely"
    elif isinstance(error, submission.InputSnapshotError):
        message = "Caller inputs could not be snapshotted completely"
    else:
        message = str(error) or "The submission request is invalid"
        private_root = str(paths.service_root())
        if private_root and private_root in message:
            message = "Caller input storage is unsafe or unavailable"
    return JSONResponse(
        status_code=400,
        content={
            "error": "request_rejected",
            "message": message,
            "details": [],
        },
    )


def _submission_failure_response(error: Exception) -> JSONResponse:
    logger.exception("Submission failed before acknowledgement: %s", error)
    return JSONResponse(
        status_code=500,
        content={
            "error": "service_failure",
            "message": "Submission could not be completed safely",
            "details": [],
        },
    )


def _single_text_field(
    fields: dict[str, list[object]],
    name: str,
    *,
    required: bool,
    default: str | None = None,
) -> str:
    values = fields.get(name, [])
    if not values:
        if required:
            raise ValueError(f"exactly one text {name} field is required")
        if default is None:
            raise ValueError(f"no default is defined for {name}")
        return default
    if len(values) != 1 or not isinstance(values[0], str):
        raise ValueError(f"exactly one text {name} field is allowed")
    return values[0]


def _submission_form_values(
    form: FormData,
) -> tuple[str, str, bool, list[submission.Upload]]:
    allowed = {"event_id", "configuration", "overwrite", "files"}
    fields: dict[str, list[object]] = {}
    for name, value in form.multi_items():
        if name not in allowed:
            raise ValueError(f"unexpected multipart field: {name!r}")
        fields.setdefault(name, []).append(value)

    event_id = _single_text_field(fields, "event_id", required=True)
    configuration = _single_text_field(
        fields,
        "configuration",
        required=False,
        default="global",
    )
    overwrite_text = _single_text_field(
        fields,
        "overwrite",
        required=False,
        default="true",
    )
    if overwrite_text not in {"true", "false"}:
        raise ValueError("overwrite must be exactly 'true' or 'false'")

    uploads: list[submission.Upload] = []
    for value in fields.get("files", []):
        if not isinstance(value, UploadFile):
            raise ValueError("every files field must be a file upload")
        uploads.append(submission.Upload(value.filename, value.file))
    return event_id, configuration, overwrite_text == "true", uploads


@app.get("/config")
def get_config():
    """Return effective service identity and operational settings."""
    try:
        return build_config_response()
    except ServiceInformationError as exc:
        return _service_information_error_response(exc)


@app.get("/configurations")
def get_configurations():
    """Return discovered configuration directory names."""
    try:
        return build_configurations_response()
    except ServiceInformationError as exc:
        return _service_information_error_response(exc)


@app.post("/events", openapi_extra=SUBMISSION_OPENAPI)
async def submit_event_endpoint(request: Request) -> JSONResponse:
    """Validate, snapshot, and durably acknowledge one multipart submission."""
    try:
        readiness = service_information.read_readiness()
        ready = readiness["ready"]
        reason = readiness["reason"]
        if not isinstance(ready, bool):
            raise ServiceInformationError("recorded readiness is not boolean")
        if ready and reason is not None:
            raise ServiceInformationError("ready state has a non-null reason")
        if not ready and (not isinstance(reason, str) or not reason):
            raise ServiceInformationError("not-ready state has no reason")
    except ServiceInformationError as exc:
        return _service_information_error_response(exc)
    except Exception:
        logger.exception("Recorded readiness is unavailable")
        return _service_information_error_response(
            ServiceInformationError("recorded readiness is unavailable")
        )

    if not ready:
        return _service_unavailable_response(reason)

    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if media_type.strip().lower() != "multipart/form-data":
        return _request_rejected_response(
            ValueError("Content-Type must be multipart/form-data")
        )

    try:
        form = await request.form(
            max_files=math.inf,
            max_fields=math.inf,
            max_part_size=math.inf,
        )
    except (StarletteHTTPException, MultiPartException, MultipartParseError):
        return _request_rejected_response(ValueError("Malformed multipart request"))
    except OSError:
        return _request_rejected_response(
            ValueError("Caller uploads could not be received safely")
        )
    except Exception as exc:
        return _submission_failure_response(exc)

    accepted = None
    try:
        try:
            event_id, configuration, overwrite, uploads = _submission_form_values(form)
        except ValueError as exc:
            return _request_rejected_response(exc)
        except Exception as exc:
            return _submission_failure_response(exc)
        try:
            accepted = await run_in_threadpool(
                submission.accept_ready_request,
                event_id,
                uploads,
                configuration=configuration,
                overwrite=overwrite,
                readiness_reader=service_information.read_readiness,
            )
        except (
            submission.InputValidationError,
            submission.InputPublicationError,
            submission.InputSnapshotError,
        ) as exc:
            return _request_rejected_response(exc)
        except submission.ServiceUnavailableError as exc:
            return _service_unavailable_response(str(exc))
        except Exception as exc:
            return _submission_failure_response(exc)
    finally:
        try:
            await form.close()
        except Exception:
            logger.exception("Multipart form cleanup failed")

    encoded_event_id = quote(accepted.event_id, safe="")
    return JSONResponse(
        status_code=202,
        content={
            "event_id": accepted.event_id,
            "internal_sequence": accepted.internal_sequence,
            "status": "QUEUED",
            "job_completed": False,
            "products_ready": False,
            "status_url": f"/events/{encoded_event_id}",
            "products_url": f"/events/{encoded_event_id}/products",
            "shared_input_path": accepted.shared_input_path,
            "shared_products_path": accepted.shared_products_path,
            "requested_configuration": accepted.requested_configuration,
            "overwrite": accepted.overwrite,
            "warnings": list(accepted.warnings),
        },
    )


@app.get("/healthz")
def healthz():
    """Return recorded readiness and installed ShakeMap version."""
    try:
        return build_health_response()
    except ServiceInformationError as exc:
        return _service_information_error_response(exc)


# ------------------------------------------------------------------
# GET /events -- current and queued calculations
# ------------------------------------------------------------------

@app.get("/events")
def list_events():
    """Return the sequence-ordered operational calculation collection."""
    try:
        return build_operational_views().events
    except DurableStateError as exc:
        return _durable_state_error_response(exc)


# ------------------------------------------------------------------
# GET /events/{event_id} -- single event detail
# ------------------------------------------------------------------

@app.get("/events/{event_id}")
def get_event(event_id: str):
    """Return current, waiting, and retained records for one event identity."""
    try:
        return build_event_detail(event_id)
    except UnknownEventError:
        return _unknown_event_response(event_id)
    except DurableStateError as exc:
        return _durable_state_error_response(exc)


# ------------------------------------------------------------------
# GET /events/{event_id}/products -- event product files listing
# ------------------------------------------------------------------

@app.get("/events/{event_id}/products")
def get_event_products(event_id: str):
    """Return the current service-owned product-manifest summary."""
    try:
        return build_current_product_summary(event_id)
    except UnknownEventError:
        return _unknown_event_response(event_id)
    except DurableStateError as exc:
        return _durable_state_error_response(exc)


# ------------------------------------------------------------------
# GET /queue -- current queue state
# ------------------------------------------------------------------

@app.get("/queue")
def get_queue():
    """Return queued calculations and capacity derived from durable state."""
    try:
        return build_operational_views().queue
    except DurableStateError as exc:
        return _durable_state_error_response(exc)
