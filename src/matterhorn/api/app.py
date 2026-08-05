from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from datetime import datetime
from email import policy
from email.parser import BytesParser
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from matterhorn.api.limits import MemoryRateLimiter
from matterhorn.api.models import (
    ActivityEventResponse,
    AddCardsRequest,
    AddMessagesRequest,
    AIConfigRequest,
    AIStatusResponse,
    AITestResponse,
    ChatRequest,
    ChatResponse,
    ConnectionsResponse,
    ConsoleConfigResponse,
    CorrectionInput,
    HealthResponse,
    IngestResponse,
    MailAccountsResponse,
    MailConfigRequest,
    MailConfigResponse,
    MailDeleteResponse,
    MailResetRequest,
    MailResetResponse,
    MailStatusResponse,
    MailSyncRequest,
    MailSyncResponse,
    MatterDetailResponse,
    MatterListResponse,
    QuickMessageRequest,
    RawIngestRequest,
    ReviewItemResponse,
    ReviewListResponse,
    ReviewResolveInput,
    ScopeListResponse,
    StreamListResponse,
    SubjectHandleBindInput,
    SubjectHandleListResponse,
    SubjectHandleResponse,
    SubjectHandleUnbindInput,
    SubjectListResponse,
    SubjectMergeInput,
    SubjectUnmergeInput,
    UnifiedMatterListResponse,
    ValueListResponse,
)
from matterhorn.connectors.mail import (
    MAIL_PROVIDERS,
    MailConfig,
    MailRuntimeRegistry,
)
from matterhorn.contracts import (
    Assertion,
    ChangeEvent,
    ExportEnvelope,
    TaskReceipt,
    TaskResult,
)
from matterhorn.errors import ChatUnavailableError, MatterhornError
from matterhorn.runtime_ai import AIConfig, AIRuntime
from matterhorn.scheduler import ServiceScheduler
from matterhorn.service import MatterhornService

MAX_UPLOAD_BYTES = 200_000
MAX_MULTIPART_BYTES = MAX_UPLOAD_BYTES + 64_000


def create_app(
    *,
    engine: Any = None,
    service: MatterhornService | None = None,
    quiet_period_minutes: float | None = None,
    max_batch_delay_minutes: float | None = None,
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
    mail_runtime: Any = None,
    mail_config_path: str | Path | None = None,
    mail_imap_ssl_factory: Any = None,
    mail_imap_factory: Any = None,
    ai_runtime: Any = None,
    ai_config_path: str | Path | None = None,
    ai_gateway_factory: Any = None,
) -> FastAPI:
    if service is None:
        if engine is None:
            raise ValueError("create_app requires engine or service")
        service = MatterhornService(engine)
    if mail_runtime is None:
        runtime_kwargs: dict[str, Any] = {
            "config_path": mail_config_path or Path.cwd() / "matterhorn.toml",
        }
        if mail_imap_ssl_factory is not None:
            runtime_kwargs["imap_ssl_factory"] = mail_imap_ssl_factory
        if mail_imap_factory is not None:
            runtime_kwargs["imap_factory"] = mail_imap_factory
        mail_runtime = MailRuntimeRegistry(service.engine, **runtime_kwargs)
    if ai_runtime is None:
        ai_runtime_kwargs: dict[str, Any] = {
            "config_path": ai_config_path
            or mail_config_path
            or Path.cwd() / "matterhorn.toml",
        }
        if ai_gateway_factory is not None:
            ai_runtime_kwargs["gateway_factory"] = ai_gateway_factory
        ai_runtime = AIRuntime(service.engine, **ai_runtime_kwargs)

    @asynccontextmanager
    async def service_lifespan(application: FastAPI):
        task = None
        scheduler = (
            ServiceScheduler(
                service.engine,
                quiet_period_minutes=quiet_period_minutes,
                max_batch_delay_minutes=max_batch_delay_minutes,
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
        if scheduler is not None or dispatcher is not None or mail_runtime is not None:

            async def loop() -> None:
                while True:
                    if scheduler is not None:
                        await asyncio.to_thread(scheduler.tick)
                    if dispatcher is not None:
                        await dispatcher.deliver_pending()
                    if mail_runtime is not None:
                        await asyncio.to_thread(mail_runtime.tick)
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

    from matterhorn.mcp import create_server

    mcp_server = create_server(service)
    mcp_http_app = mcp_server.streamable_http_app()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with (
            mcp_server.session_manager.run(),
            service_lifespan(application),
        ):
            yield

    app = FastAPI(
        title="Matterhorn Memory API",
        version="0.6.0",
        description="Deterministic, evidence-backed temporal memory protocol.",
        lifespan=lifespan,
    )
    app.state.matterhorn_service = service
    app.state.scheduler_task = None
    app.state.console_chat_runner = (
        chat_runner if chat_runner is not None else ai_runtime.chat_runner
    )
    app.state.mail_runtime = mail_runtime
    app.state.ai_runtime = ai_runtime
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
        details = [
            {
                key: value
                for key, value in item.items()
                if key in {"type", "loc", "msg"}
            }
            for item in error.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "REQUEST_VALIDATION_ERROR",
                    "message": str(details),
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

    @app.get(
        "/v1/stream",
        response_model=StreamListResponse,
        summary="Read the newest non-revoked staged Records across scopes",
    )
    def stream(limit: int = 50):
        return service.recent_stream(limit=limit)

    @app.get(
        "/v1/scopes/{scope_id}/stream",
        response_model=StreamListResponse,
        summary="Read the newest non-revoked staged Records in one scope",
    )
    def scoped_stream(scope_id: str, limit: int = 50):
        return service.recent_stream(scope_id=scope_id, limit=limit)

    @app.get(
        "/v1/events",
        response_model=list[ActivityEventResponse],
        summary="List the latest projection changes across every scope",
    )
    def activity(since: datetime | None = None, limit: int = 50):
        return service.activity(since=since, limit=limit)

    @app.get(
        "/v1/connections",
        response_model=ConnectionsResponse,
        summary="Report hub inputs, per-scope ingestion, and distill backlog",
    )
    def connections():
        mail_accounts = (
            app.state.mail_runtime.accounts()
            if hasattr(app.state.mail_runtime, "accounts")
            else []
        )
        return {
            **service.connections(),
            "mail": app.state.mail_runtime.status(),
            "mail_accounts": mail_accounts,
            "ai": app.state.ai_runtime.status(),
        }

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
        "/v1/scopes/{scope_id}/upload",
        response_model=IngestResponse,
        summary="Upload and immediately extract mbox, EML, YAML, or JSON",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "multipart/form-data": {
                        "schema": {
                            "type": "object",
                            "required": ["file"],
                            "properties": {
                                "file": {"type": "string", "format": "binary"}
                            },
                        }
                    }
                },
            }
        },
    )
    async def upload(scope_id: str, request: Request):
        ingest_limiter.check(_rate_key(request, "upload"))
        filename, content = await _multipart_upload(request)
        return service.upload_raw(
            scope_id=scope_id,
            filename=filename,
            content=content,
        )

    @app.post(
        "/v1/scopes/{scope_id}/quick-message",
        response_model=TaskReceipt | TaskResult,
        summary="Jot one message, defaulting sent_at on the server",
    )
    def quick_message(
        scope_id: str,
        payload: QuickMessageRequest,
        request: Request,
    ):
        ingest_limiter.check(_rate_key(request, "quick-message"))
        return service.quick_message(
            scope_id=scope_id,
            sender=payload.sender,
            text=payload.text,
            sent_at=payload.sent_at,
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
        "/v1/matters",
        response_model=UnifiedMatterListResponse,
        summary="List matters across every scope, tagged with scope_id",
    )
    def all_matters(scope: str | None = None):
        return service.all_matters(scope_id=scope)

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

    @app.post(
        "/v1/scopes/{scope_id}/merges",
        response_model=ChangeEvent,
        summary="Merge one subject into another with human provenance",
    )
    def merge_subjects(scope_id: str, payload: SubjectMergeInput):
        return service.merge_subjects(
            scope_id=scope_id,
            source_subject_key=payload.source_subject_key,
            target_subject_key=payload.target_subject_key,
            source_refs=payload.source_refs,
        )

    @app.post(
        "/v1/scopes/{scope_id}/merges/{source_subject_key}/unmerge",
        response_model=ChangeEvent,
        summary="Reverse an active subject merge with human provenance",
    )
    def unmerge_subject(
        scope_id: str,
        source_subject_key: str,
        payload: SubjectUnmergeInput,
    ):
        return service.unmerge_subjects(
            scope_id=scope_id,
            source_subject_key=source_subject_key,
            source_refs=payload.source_refs,
        )

    @app.get(
        "/v1/scopes/{scope_id}/subjects/{subject_key}/handles",
        response_model=SubjectHandleListResponse,
        summary="List active canonicalized handles for one subject",
    )
    def subject_handles(scope_id: str, subject_key: str):
        return service.subject_handles(
            scope_id=scope_id,
            subject_key=subject_key,
        )

    @app.post(
        "/v1/scopes/{scope_id}/subjects/{subject_key}/handles",
        response_model=SubjectHandleResponse,
        summary="Bind an evidenced handle to a subject",
    )
    def bind_handle(
        scope_id: str,
        subject_key: str,
        payload: SubjectHandleBindInput,
    ):
        return service.bind_handle(
            scope_id=scope_id,
            subject_key=subject_key,
            handle_type=payload.handle_type,
            handle_value=payload.handle_value,
            source_refs=payload.source_refs,
        )

    @app.post(
        "/v1/scopes/{scope_id}/subjects/{subject_key}/handles/"
        "{handle_type}/{normalized_value:path}/unbind",
        response_model=SubjectHandleResponse,
        summary="Revoke an active handle binding with human provenance",
    )
    def unbind_handle(
        scope_id: str,
        subject_key: str,
        handle_type: str,
        normalized_value: str,
        payload: SubjectHandleUnbindInput,
    ):
        return service.unbind_handle(
            scope_id=scope_id,
            subject_key=subject_key,
            handle_type=handle_type,
            normalized_value=normalized_value,
            source_refs=payload.source_refs,
        )

    @app.get(
        "/v1/scopes/{scope_id}/reviews",
        response_model=ReviewListResponse,
        summary="List pending identity-routing reviews",
    )
    def review_items(scope_id: str):
        return service.review_items(scope_id=scope_id)

    @app.post(
        "/v1/scopes/{scope_id}/reviews/{review_id}/resolve",
        response_model=ReviewItemResponse,
        summary="Resolve one pending identity-routing review",
    )
    def resolve_review(
        scope_id: str,
        review_id: str,
        payload: ReviewResolveInput,
    ):
        return service.resolve_review(
            scope_id=scope_id,
            review_id=review_id,
            action=payload.action,
            subject_key=payload.subject_key,
            source_refs=payload.source_refs,
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
        "/v1/connectors/mail/config",
        response_model=MailConfigResponse,
        summary="Persist non-secret IMAP settings and load a password in memory",
    )
    def mail_config(payload: MailConfigRequest):
        config = _mail_config_from_request(payload)
        configure = getattr(
            app.state.mail_runtime,
            "configure_first",
            app.state.mail_runtime.configure,
        )
        return configure(config, password=payload.password)

    @app.get(
        "/v1/connectors/mail/accounts",
        response_model=MailAccountsResponse,
        summary="List every configured mailbox with redacted independent state",
    )
    def mail_accounts():
        return app.state.mail_runtime.accounts()

    @app.post(
        "/v1/connectors/mail/accounts",
        response_model=MailConfigResponse,
        summary="Create or update a mailbox; keep its password in memory only",
    )
    def upsert_mail_account(payload: MailConfigRequest):
        return app.state.mail_runtime.configure(
            _mail_config_from_request(payload),
            password=payload.password,
        )

    @app.delete(
        "/v1/connectors/mail/accounts/{account_id:path}",
        response_model=MailDeleteResponse,
        summary=(
            "Remove mailbox configuration and memory credential; retain "
            "stored watermark data"
        ),
    )
    def delete_mail_account(account_id: str):
        return app.state.mail_runtime.delete(account_id)

    @app.post(
        "/v1/connectors/mail/accounts/{account_id:path}/sync",
        response_model=MailSyncResponse,
        summary="Synchronize one mailbox above its independent UID watermark",
    )
    def sync_mail_account(
        account_id: str,
        payload: MailSyncRequest,
        request: Request,
    ):
        ingest_limiter.check(_rate_key(request, f"mail-sync:{account_id}"))
        return app.state.mail_runtime.sync_account(
            account_id,
            scope_id=payload.scope_id,
            backfill=payload.backfill,
        ).to_dict()

    @app.post(
        "/v1/connectors/mail/accounts/{account_id:path}/reset",
        response_model=MailResetResponse,
        summary="Forget one mailbox UID position after explicit confirmation",
    )
    def reset_mail_account(
        account_id: str,
        payload: MailResetRequest,
        request: Request,
    ):
        ingest_limiter.check(_rate_key(request, f"mail-reset:{account_id}"))
        return app.state.mail_runtime.reset_account(
            account_id,
            scope_id=payload.scope_id,
            confirm=payload.confirm,
        )

    @app.get(
        "/v1/connectors/mail/status",
        response_model=MailStatusResponse,
        summary="Read redacted mail connector state and UID position",
    )
    def mail_status(scope_id: str | None = None):
        return app.state.mail_runtime.status(scope_id=scope_id)

    @app.post(
        "/v1/connectors/mail/sync",
        response_model=MailSyncResponse,
        summary="Pull UIDs above the stored watermark and flush them",
    )
    def mail_sync(payload: MailSyncRequest, request: Request):
        ingest_limiter.check(_rate_key(request, "mail-sync"))
        return app.state.mail_runtime.sync(
            scope_id=payload.scope_id,
            backfill=payload.backfill,
        ).to_dict()

    @app.post(
        "/v1/connectors/mail/reset",
        response_model=MailResetResponse,
        summary="Forget the configured UID position after explicit confirmation",
    )
    def mail_reset(payload: MailResetRequest, request: Request):
        ingest_limiter.check(_rate_key(request, "mail-reset"))
        return app.state.mail_runtime.reset(
            scope_id=payload.scope_id,
            confirm=payload.confirm,
        )

    @app.get(
        "/v1/connectors/ai/status",
        response_model=AIStatusResponse,
        summary="Read redacted runtime AI configuration and key state",
    )
    def ai_status():
        return app.state.ai_runtime.status()

    @app.post(
        "/v1/connectors/ai/config",
        response_model=AIStatusResponse,
        summary=(
            "Persist non-secret AI settings, retain the key in memory, and "
            "rewire subsequent write extraction and Console chat"
        ),
    )
    def ai_config(payload: AIConfigRequest):
        result = app.state.ai_runtime.configure(
            _ai_config_from_request(payload),
            api_key=payload.api_key,
        )
        app.state.console_chat_runner = app.state.ai_runtime.chat_runner
        return result

    @app.post(
        "/v1/connectors/ai/test",
        response_model=AITestResponse,
        summary="Probe an AI provider without persisting candidate settings",
    )
    def ai_test(payload: AIConfigRequest):
        return app.state.ai_runtime.test(
            _ai_config_from_request(payload),
            api_key=payload.api_key,
        )

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
        # Deterministic UI version: changes exactly when the page assets
        # change, so open tabs can detect a redeploy and offer a reload —
        # a restart without UI changes never nags.
        asset_version = hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]

        @app.get("/", include_in_schema=False)
        def console_root():
            return RedirectResponse("/console")

        @app.get("/console", response_class=HTMLResponse, include_in_schema=False)
        def console_page():
            return HTMLResponse(template)

        @app.get("/v1/console/version")
        def console_version() -> dict[str, str]:
            return {"version": asset_version}

    # Keep the MCP SDK's own /mcp route exact and last so public REST routes
    # remain authoritative while sharing one ASGI process and one store owner.
    app.mount("/", mcp_http_app, name="mcp")
    return app


def _mail_config_from_request(payload: MailConfigRequest) -> MailConfig:
    if (
        payload.account_id is not None
        and payload.name is not None
        and payload.account_id != payload.name
    ):
        raise ValueError("mail account_id and name MUST match when both are set")
    preset = MAIL_PROVIDERS.get(payload.provider)
    return MailConfig(
        provider=payload.provider,
        host=payload.host or (preset.host if preset is not None else ""),
        port=payload.port or (preset.port if preset is not None else 993),
        ssl=(
            payload.ssl
            if payload.ssl is not None
            else (preset.ssl if preset is not None else True)
        ),
        user=payload.user,
        folder=payload.folder,
        interval=payload.interval,
        initial_window=payload.initial_window,
        scope=payload.scope,
        name=payload.account_id or payload.name,
    )


def _ai_config_from_request(payload: AIConfigRequest) -> AIConfig:
    return AIConfig(
        provider=payload.provider,
        base_url=payload.base_url,
        model=payload.model,
        timeout=payload.timeout,
    )


def _rate_key(request: Request, resource: str) -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{resource}:{host}"


async def _multipart_upload(request: Request) -> tuple[str, bytes]:
    content_type = request.headers.get("content-type", "")
    if not content_type.casefold().startswith("multipart/form-data"):
        raise ValueError("Upload requires multipart/form-data.")
    declared = request.headers.get("content-length")
    if declared is not None and int(declared) > MAX_MULTIPART_BYTES:
        raise ValueError(f"Upload exceeds the {MAX_UPLOAD_BYTES}-byte input cap.")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_MULTIPART_BYTES:
            raise ValueError(f"Upload exceeds the {MAX_UPLOAD_BYTES}-byte input cap.")
        chunks.append(chunk)
    envelope = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode(
            "ascii"
        )
        + b"".join(chunks)
    )
    message = BytesParser(policy=policy.default).parsebytes(envelope)
    if not message.is_multipart():
        raise ValueError("Upload multipart body is invalid.")
    for part in message.iter_parts():
        if part.get_param("name", header="content-disposition") != "file":
            continue
        filename = part.get_filename()
        content = part.get_payload(decode=True)
        if content is None and part.get_content_type() == "message/rfc822":
            nested = part.get_payload()
            if isinstance(nested, list) and nested:
                content = nested[0].as_bytes(policy=policy.default)
        if not filename or content is None:
            raise ValueError("Upload file and filename are required.")
        if len(content) > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"Upload exceeds the {MAX_UPLOAD_BYTES}-byte input cap."
            )
        return filename, content
    raise ValueError("Upload multipart body requires a file field.")
