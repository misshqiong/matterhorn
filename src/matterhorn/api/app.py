from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from importlib import resources
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from matterhorn.api.limits import MemoryRateLimiter
from matterhorn.api.models import (
    AddCardsRequest,
    AddMessagesRequest,
    ChatRequest,
    ChatResponse,
    ConsoleConfigResponse,
    CorrectionInput,
    HealthResponse,
    IngestResponse,
    MatterDetailResponse,
    MatterListResponse,
    RawIngestRequest,
    ScopeListResponse,
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
from matterhorn.errors import ChatUnavailableError, MatterhornError
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
    console_enabled: bool = False,
    chat_runner: Any = None,
    ingest_rate_limit: int = 20,
    chat_rate_limit: int = 30,
    rate_limit_window_seconds: float = 60,
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
    app.state.console_chat_runner = chat_runner
    ingest_limiter = MemoryRateLimiter(
        limit=ingest_rate_limit,
        window_seconds=rate_limit_window_seconds,
    )
    chat_limiter = MemoryRateLimiter(
        limit=chat_rate_limit,
        window_seconds=rate_limit_window_seconds,
    )

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

    @app.get(
        "/v1/scopes",
        response_model=ScopeListResponse,
        summary="List scopes present in the configured store",
    )
    def scopes():
        return service.list_scopes()

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
        "/v1/scopes/{scope_id}/ingest",
        response_model=IngestResponse,
        summary="Auto-detect and ingest pasted chat, messages, or email",
    )
    def ingest(scope_id: str, payload: RawIngestRequest, request: Request):
        ingest_limiter.check(_rate_key(request, "ingest"))
        return service.ingest_raw(
            scope_id=scope_id,
            text=payload.text,
            wait=payload.wait,
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
        "/v1/scopes/{scope_id}/matters/{subject_key}",
        response_model=MatterDetailResponse,
        summary="Read current values, timelines, and evidence for one matter",
    )
    def matter_detail(scope_id: str, subject_key: str):
        return service.matter_detail(
            scope_id=scope_id,
            subject_key=subject_key,
        )

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

    @app.get(
        "/v1/console/config",
        response_model=ConsoleConfigResponse,
        summary="Report optional Console capabilities without exposing secrets",
    )
    def console_config():
        runner = app.state.console_chat_runner
        return {
            "chat_enabled": runner is not None,
            "chat_provider": runner.provider if runner is not None else None,
        }

    @app.post(
        "/v1/scopes/{scope_id}/chat",
        response_model=ChatResponse,
        summary="Ask a provider using only deterministic scoped query tools",
    )
    def chat(scope_id: str, payload: ChatRequest, request: Request):
        chat_limiter.check(_rate_key(request, "chat"))
        runner = app.state.console_chat_runner
        if runner is None:
            raise ChatUnavailableError(
                "Console chat requires a configured provider key and model."
            )
        return service.chat(
            scope_id=scope_id,
            message=payload.message,
            history=[
                item.model_dump(mode="python") for item in payload.history
            ],
            runner=runner,
        )

    if console_enabled:
        template = (
            resources.files("matterhorn")
            .joinpath("templates/console.html.j2")
            .read_text(encoding="utf-8")
        )

        @app.get("/", include_in_schema=False)
        def console_root():
            return RedirectResponse("/console")

        @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
        def console_page():
            return HTMLResponse(template)

    return app


def _rate_key(request: Request, resource: str) -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{resource}:{host}"
