from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from matterhorn.api.models import (
    AddEpisodeCardsRequest,
    AtRequest,
    ByPersonRequest,
    CorrectRequest,
    HealthResponse,
    ListMattersRequest,
    MutationResponse,
    PredicateRequest,
    SubjectListResponse,
    ValueListResponse,
)
from matterhorn.contracts import Assertion
from matterhorn.service import MatterhornService


def create_app(*, engine: Any = None, service: MatterhornService | None = None) -> FastAPI:
    if service is None:
        if engine is None:
            raise ValueError("create_app requires engine or service")
        service = MatterhornService(engine)

    app = FastAPI(
        title="Matterhorn Memory API",
        version="0.3.0",
        description="Deterministic, evidence-backed temporal memory protocol.",
    )
    app.state.matterhorn_service = service

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

    @app.post("/v1/add_episode_cards", response_model=MutationResponse)
    def add_episode_cards(request: AddEpisodeCardsRequest):
        return service.add_episode_cards(
            cards=request.cards, scope_id=request.scope_id
        )

    @app.post("/v1/query_current", response_model=ValueListResponse)
    def query_current(request: PredicateRequest):
        return service.query_current(**request.model_dump())

    @app.post("/v1/query_timeline", response_model=ValueListResponse)
    def query_timeline(request: PredicateRequest):
        return service.query_timeline(**request.model_dump())

    @app.post("/v1/query_at", response_model=ValueListResponse)
    def query_at(request: AtRequest):
        return service.query_at(**request.model_dump())

    @app.post("/v1/query_by_person", response_model=SubjectListResponse)
    def query_by_person(request: ByPersonRequest):
        return service.query_by_person(**request.model_dump())

    @app.post("/v1/list_matters", response_model=SubjectListResponse)
    def list_matters(request: ListMattersRequest):
        return service.list_matters(**request.model_dump())

    @app.post("/v1/correct", response_model=Assertion)
    def correct(request: CorrectRequest):
        return service.correct(correction=request.correction)

    return app
