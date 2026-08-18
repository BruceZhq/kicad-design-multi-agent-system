from __future__ import annotations

import hmac
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, SerializeAsAny

from core import settings
from schema import (
    AllModelEnum,
    ChatHistory,
    ChatHistoryInput,
    RunCancelResponse,
    RunStatus,
    ServiceMetadata,
    StreamInput,
)
from service.internal_auth import (
    InternalClaims,
    InternalTokenError,
    require_run_id,
    verify_internal_token,
)
from service.runtime_identity import scope_identity

_AGENT_ID = "ratsnestpro-multi-agent"


class InternalStreamRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=100_000)
    model: SerializeAsAny[AllModelEnum] | None = None
    thread_id: str = Field(min_length=1, max_length=200)
    request_id: str = Field(min_length=8, max_length=200)
    timeout_seconds: float | None = Field(default=None, ge=1, le=86_400)
    agent_config: dict[str, Any] = Field(default_factory=dict)
    stream_tokens: bool = True
    last_event_id: int = Field(default=0, ge=0)

    def to_runtime_input(self, claims: InternalClaims) -> StreamInput:
        runtime_input = StreamInput(**self.model_dump(), user_id=claims.subject)
        runtime_input.bind_runtime_identity(
            principal_id=claims.subject,
            tenant_id=claims.tenant_id,
            project_id=claims.project_id,
        )
        return runtime_input


class InternalHistoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=8, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)


class InternalInteractionResponseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    response_request_id: UUID
    answer: str = Field(min_length=1, max_length=100_000)
    state_version: int = Field(ge=1)
    model: SerializeAsAny[AllModelEnum] | None = None
    timeout_seconds: float | None = Field(default=None, ge=1, le=86_400)
    agent_config: dict[str, Any] = Field(default_factory=dict)


router = APIRouter(prefix="/internal/v1")


async def verified_internal_claims(request: Request) -> InternalClaims:
    secret = settings.RATSNEST_INTERNAL_SIGNING_SECRET
    if secret is None or len(secret.get_secret_value().encode("utf-8")) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal authentication is not configured.",
        )
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if (
        not separator
        or not token
        or not hmac.compare_digest(scheme.casefold(), "bearer")
    ):
        raise _unauthorized()
    try:
        return verify_internal_token(
            token,
            secret=secret.get_secret_value(),
            issuer=settings.RATSNEST_INTERNAL_JWT_ISSUER,
            audience=settings.RATSNEST_INTERNAL_JWT_AUDIENCE,
            method=request.method,
            path=request.url.path,
            body=await request.body(),
            clock_skew_seconds=settings.RATSNEST_INTERNAL_JWT_CLOCK_SKEW_SECONDS,
            max_ttl_seconds=settings.RATSNEST_INTERNAL_JWT_MAX_TTL_SECONDS,
        )
    except InternalTokenError as exc:
        raise _unauthorized() from exc


def _require_request_run(claims: InternalClaims, request_id: str) -> None:
    try:
        require_run_id(claims, request_id)
    except InternalTokenError as exc:
        raise _unauthorized() from exc


def _owner_id(claims: InternalClaims) -> str:
    return scope_identity(
        claims.subject,
        claims.tenant_id,
        claims.project_id,
    ).owner_id


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid internal credential.",
        headers={"WWW-Authenticate": "Bearer"},
    )


@router.get("/info")
async def internal_info(
    _claims: Annotated[InternalClaims, Depends(verified_internal_claims)],
) -> ServiceMetadata:
    from agents import get_all_agent_info
    from agents.ratsnestpro.profiles import get_profile_metadata

    agent = next(info for info in get_all_agent_info() if info.key == _AGENT_ID)
    models = sorted(settings.AVAILABLE_MODELS)
    return ServiceMetadata(
        agents=[agent],
        models=models,
        default_agent=_AGENT_ID,
        default_model=settings.DEFAULT_MODEL,
        profiles=get_profile_metadata(),
    )


@router.post("/runs/ratsnestpro-multi-agent/stream", response_class=StreamingResponse)
async def internal_stream(
    input: InternalStreamRequest,
    claims: Annotated[InternalClaims, Depends(verified_internal_claims)],
) -> StreamingResponse:
    _require_request_run(claims, input.request_id)
    from service.service import stream

    return await stream(input.to_runtime_input(claims), _AGENT_ID)


@router.get("/runs/{request_id}")
async def internal_run_status(
    request_id: str,
    claims: Annotated[InternalClaims, Depends(verified_internal_claims)],
) -> RunStatus:
    _require_request_run(claims, request_id)
    from service.service import run_status

    return await run_status(request_id, _owner_id(claims))


@router.post("/runs/{request_id}/interactions/{interaction_id}/responses")
async def internal_resume_interaction(
    request_id: str,
    interaction_id: Annotated[
        str,
        Path(pattern=r"^[A-Za-z0-9._:-]{1,200}$"),
    ],
    input: InternalInteractionResponseRequest,
    claims: Annotated[InternalClaims, Depends(verified_internal_claims)],
) -> RunStatus:
    _require_request_run(claims, request_id)
    from service.service import resume_interaction, run_status

    current = await run_status(request_id, _owner_id(claims))
    runtime_input = StreamInput(
        message=input.answer,
        model=input.model,
        thread_id=current.thread_id,
        user_id=claims.subject,
        request_id=request_id,
        timeout_seconds=input.timeout_seconds,
        agent_config=input.agent_config,
        stream_tokens=True,
    )
    runtime_input.bind_runtime_identity(
        principal_id=claims.subject,
        tenant_id=claims.tenant_id,
        project_id=claims.project_id,
    )
    return await resume_interaction(
        runtime_input,
        interaction_id=interaction_id,
        response_request_id=str(input.response_request_id),
        state_version=input.state_version,
        agent_id=_AGENT_ID,
    )


@router.delete("/runs/{request_id}")
async def internal_cancel_run(
    request_id: str,
    claims: Annotated[InternalClaims, Depends(verified_internal_claims)],
) -> RunCancelResponse:
    _require_request_run(claims, request_id)
    from service.service import cancel_run

    return await cancel_run(request_id, _owner_id(claims))


@router.post("/history")
async def internal_history(
    input: InternalHistoryRequest,
    claims: Annotated[InternalClaims, Depends(verified_internal_claims)],
) -> ChatHistory:
    _require_request_run(claims, input.request_id)
    from service.service import history

    history_input = ChatHistoryInput(thread_id=input.thread_id, user_id=claims.subject)
    history_input.bind_runtime_identity(
        principal_id=claims.subject,
        tenant_id=claims.tenant_id,
        project_id=claims.project_id,
    )
    return await history(history_input, _AGENT_ID)
