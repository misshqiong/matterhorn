from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from matterhorn.api.models import (
    AddCardsRequest,
    AddMessagesRequest,
    CorrectionInput,
    HealthResponse,
    MatterListResponse,
    SubjectListResponse,
    ValueListResponse,
)
from matterhorn.contracts import (
    Assertion,
    ChangeEvent,
    ExportEnvelope,
    TaskReceipt,
    TaskResult,
)
from matterhorn.errors import MatterhornError
from matterhorn.scheduler import ServiceScheduler
from matterhorn.service import MatterhornService


def create_app(
    *,
    engine: Any = None,
    service: MatterhornService | None = None,
    quiet_period_minutes: float | None = None,
    daily_flush_at: str | None = None,
    scheduler_clock: Any = None,
    scheduler_poll_seconds: float = 30,
    webhook_url: str | None = None,
    webhook_transport: Any = None,
    webhook_max_attempts: int = 3,
    webhook_backoff_seconds: float = 1,
) -> FastAPI:
    if service is None:
        if engine is None:
            raise ValueError("create_app requires engine or service")
        service = MatterhornService(engine)

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        task = None
        scheduler = (
            ServiceScheduler(
                service.engine,
                quiet_period_minutes=quiet_period_minutes,
                daily_flush_at=daily_flush_at,
                clock=scheduler_clock,
            )
            if quiet_period_minutes is not None or daily_flush_at is not None
            else None
        )
        dispatcher = None
        if webhook_url is not None:
            from matterhorn.webhooks import WebhookDispatcher

            dispatcher = WebhookDispatcher(
                service.engine.store,
                webhook_url,
                transport=webhook_transport,
                max_attempts=webhook_max_attempts,
                backoff_seconds=webhook_backoff_seconds,
            )
        if scheduler is not None or dispatcher is not None:

            async def loop() -> None:
                while True:
                    if scheduler is not None:
                        await asyncio.to_thread(scheduler.tick)
                    if dispatcher is not None:
                        await dispatcher.deliver_pending()
                    await asyncio.sleep(scheduler_poll_seconds)

            task = asyncio.create_task(loop())
            application.state.scheduler_task = task
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    app = FastAPI(
        title="Matterhorn Memory API",
        version="0.6.0",
        description="Deterministic, evidence-backed temporal memory protocol.",
        lifespan=lifespan,
    )
    app.state.matterhorn_service = service
    app.state.scheduler_task = None

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_ERROR",
                    "message": str(error),
                }
            },
        )

    @app.exception_handler(MatterhornError)
    async def matterhorn_error(
        _request: Request, error: MatterhornError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "message": str(error),
                }
            },
        )

    @app.exception_handler(Exception)
    async def structured_error(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "error": {
                    "code": type(error).__name__,
                    "message": str(error),
                }
            },
        )

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/v1/scopes/{scope_id}/messages",
        response_model=TaskReceipt | TaskResult,
    )
    def add_messages(scope_id: str, request: AddMessagesRequest):
        return service.add_messages(
            scope_id=scope_id,
            messages=request.messages,
            wait=request.wait,
        )

    @app.post(
        "/v1/scopes/{scope_id}/cards",
        response_model=TaskReceipt | TaskResult,
    )
    def add_cards(scope_id: str, request: AddCardsRequest):
        return service.add_cards(
            scope_id=scope_id,
            cards=[
                {
                    key: value
                    for key, value in card.model_dump(mode="python").items()
                    if value is not None or key != "scope_id"
                }
                for card in request.cards
            ],
            wait=request.wait,
        )

    @app.get(
        "/v1/scopes/{scope_id}/matters",
        response_model=MatterListResponse,
    )
    def matters(scope_id: str):
        return service.list_matters(scope_id=scope_id)

    @app.get(
        "/v1/scopes/{scope_id}/query/current",
        response_model=ValueListResponse,
    )
    def query_current(scope_id: str, subject_key: str, predicate: str):
        return service.query_current(
            scope_id=scope_id,
            subject_key=subject_key,
            predicate=predicate,
        )

    @app.get(
        "/v1/scopes/{scope_id}/query/timeline",
        response_model=ValueListResponse,
    )
    def query_timeline(scope_id: str, subject_key: str, predicate: str):
        return service.query_timeline(
            scope_id=scope_id,
            subject_key=subject_key,
            predicate=predicate,
        )

    @app.get(
        "/v1/scopes/{scope_id}/query/at",
        response_model=ValueListResponse,
    )
    def query_at(
        scope_id: str,
        subject_key: str,
        predicate: str,
        instant: datetime,
    ):
        return service.query_at(
            scope_id=scope_id,
            subject_key=subject_key,
            predicate=predicate,
            instant=instant,
        )

    @app.get(
        "/v1/scopes/{scope_id}/query/by-person",
        response_model=SubjectListResponse,
    )
    def query_by_person(scope_id: str, person_id: str):
        return service.query_by_person(
            scope_id=scope_id,
            person_id=person_id,
        )

    @app.post(
        "/v1/scopes/{scope_id}/corrections",
        response_model=Assertion,
    )
    def correct(scope_id: str, correction: CorrectionInput):
        return service.correct(
            scope_id=scope_id,
            correction=correction.model_dump(mode="python"),
        )

    @app.get("/v1/tasks/{task_id}", response_model=TaskResult)
    def task(task_id: str):
        return service.task(task_id=task_id)

    @app.get(
        "/v1/scopes/{scope_id}/events",
        response_model=list[ChangeEvent],
    )
    def events(scope_id: str, since: datetime | None = None):
        return service.events(scope_id=scope_id, since=since)

    @app.get(
        "/v1/scopes/{scope_id}/export",
        response_model=ExportEnvelope,
    )
    def export(scope_id: str):
        return service.export(scope_id=scope_id)

    return app
