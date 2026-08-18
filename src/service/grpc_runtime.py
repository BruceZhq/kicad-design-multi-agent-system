"""Signed internal gRPC adapter for the existing Agent Runtime services."""

from __future__ import annotations

import asyncio
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any, NoReturn
from uuid import UUID

import grpc
from fastapi import HTTPException
from pydantic import ValidationError
from temporalio.service import RPCError, RPCStatusCode

from core import settings
from schema import RunStatus, StreamInput
from service.internal_auth import InternalClaims, InternalTokenError, verify_internal_token
from service.proto import agent_runtime_pb2 as pb
from service.proto import agent_runtime_pb2_grpc as pb_grpc
from service.run_registry import RunAccessError, RunNotFoundError
from service.runtime_identity import scope_identity

_AGENT_ID = "ratsnestpro-multi-agent"
_SERVICE_PATH = "/ratsnest.runtime.v1.AgentRuntimeService"
_STATE = {
    "queued": pb.RUN_STATE_QUEUED,
    "running": pb.RUN_STATE_RUNNING,
    "waiting_for_input": pb.RUN_STATE_WAITING_FOR_INPUT,
    "completed": pb.RUN_STATE_COMPLETED,
    "failed": pb.RUN_STATE_FAILED,
    "cancelled": pb.RUN_STATE_CANCELLED,
    "timed_out": pb.RUN_STATE_TIMED_OUT,
}


def _owner_id(claims: InternalClaims) -> str:
    return scope_identity(
        claims.subject,
        claims.tenant_id,
        claims.project_id,
    ).owner_id


async def _abort(
    context: grpc.aio.ServicerContext[Any, Any],
    code: grpc.StatusCode,
    detail: str,
) -> NoReturn:
    await context.abort(code, detail)
    raise RuntimeError("gRPC abort unexpectedly returned")


def _authorization(context: grpc.aio.ServicerContext[Any, Any]) -> str:
    values = [
        str(item.value)
        for item in context.invocation_metadata()
        if item.key.casefold() == "authorization"
    ]
    if len(values) != 1:
        raise InternalTokenError("Exactly one authorization value is required.")
    scheme, separator, token = values[0].partition(" ")
    if not separator or not token or not hmac.compare_digest(scheme.casefold(), "bearer"):
        raise InternalTokenError("Internal authorization metadata is invalid.")
    return token


async def _authenticate(
    request: Any,
    context: grpc.aio.ServicerContext[Any, Any],
    rpc_name: str,
    request_id: str,
    identity: pb.RuntimeIdentity,
) -> InternalClaims:
    secret = settings.RATSNEST_INTERNAL_SIGNING_SECRET
    try:
        if secret is None:
            raise InternalTokenError("Internal authentication is not configured.")
        claims = verify_internal_token(
            _authorization(context),
            secret=secret.get_secret_value(),
            issuer=settings.RATSNEST_INTERNAL_JWT_ISSUER,
            audience=settings.RATSNEST_INTERNAL_JWT_AUDIENCE,
            method="POST",
            path=f"{_SERVICE_PATH}/{rpc_name}",
            body=request.SerializeToString(deterministic=True),
            clock_skew_seconds=settings.RATSNEST_INTERNAL_JWT_CLOCK_SKEW_SECONDS,
            max_ttl_seconds=settings.RATSNEST_INTERNAL_JWT_MAX_TTL_SECONDS,
        )
        expected = (
            (claims.run_id, request_id),
            (claims.subject, identity.principal_id),
            (claims.tenant_id, identity.tenant_id),
            (claims.project_id, identity.project_id),
        )
        if any(not actual or not hmac.compare_digest(actual, value) for actual, value in expected):
            raise InternalTokenError("Internal identity does not match the signed request.")
        return claims
    except InternalTokenError:
        await _abort(context, grpc.StatusCode.UNAUTHENTICATED, "Invalid internal credential.")


def _run_message(status: RunStatus) -> pb.Run:
    optional: dict[str, Any] = {}
    for source, target in (
        (status.run_id, "graph_run_id"),
        (status.started_at, "started_at"),
        (status.finished_at, "finished_at"),
        (status.oldest_event_id, "oldest_event_seq"),
        (status.newest_event_id, "newest_event_seq"),
        (status.error_code, "error_code"),
        (status.error, "error"),
    ):
        if source is not None:
            optional[target] = source.isoformat() if isinstance(source, datetime) else source
    result = {
        "artifact_manifest": status.artifact_manifest,
        "delivery_status": status.delivery_status,
    }
    if any(value is not None for value in result.values()):
        optional["result_json"] = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return pb.Run(
        request_id=status.request_id,
        kind=status.kind,
        state=_STATE[status.status],
        agent_id=status.agent_id,
        thread_id=status.thread_id,
        created_at=status.created_at.isoformat(),
        event_count=status.event_count,
        **optional,
    )


async def _status_or_abort(
    runtime: Any,
    request_id: str,
    principal_id: str,
    context: grpc.aio.ServicerContext[Any, Any],
) -> pb.Run:
    try:
        status = await runtime.run_status(request_id, principal_id)
    except HTTPException as exc:
        await _abort_http(context, exc)
    return _run_message(status)


async def _abort_http(
    context: grpc.aio.ServicerContext[Any, Any],
    exc: HTTPException,
) -> NoReturn:
    code = {
        400: grpc.StatusCode.INVALID_ARGUMENT,
        403: grpc.StatusCode.PERMISSION_DENIED,
        404: grpc.StatusCode.NOT_FOUND,
        409: grpc.StatusCode.ALREADY_EXISTS,
        422: grpc.StatusCode.INVALID_ARGUMENT,
        429: grpc.StatusCode.RESOURCE_EXHAUSTED,
        503: grpc.StatusCode.UNAVAILABLE,
        504: grpc.StatusCode.DEADLINE_EXCEEDED,
    }.get(exc.status_code, grpc.StatusCode.INTERNAL)
    await _abort(context, code, str(exc.detail))


def _config(value: str) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("config_json must contain a JSON object")
    return parsed


def _event_message(
    chunk: str,
    request_id: str,
    *,
    terminal: Any | None = None,
) -> pb.RunEvent | None:
    sequence: int | None = None
    data: list[str] = []
    for line in chunk.splitlines():
        if line.startswith("id: "):
            try:
                sequence = int(line[4:])
            except ValueError:
                return None
        elif line.startswith("data: "):
            data.append(line[6:])
    if sequence is None or not data:
        return None
    raw = "\n".join(data)
    if raw == "[DONE]":
        if terminal is None or not terminal.is_terminal:
            raise ValueError("DONE requires a verified terminal run state")
        event_type = str(terminal.status)
        payload: dict[str, Any] = {
            "type": event_type,
            "status": event_type,
            "request_id": request_id,
        }
        if terminal.error_code:
            payload["error_code"] = terminal.error_code
        if terminal.error:
            payload["error"] = terminal.error
    else:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = {"type": "raw", "content": raw}
        payload = decoded if isinstance(decoded, dict) else {"type": "raw", "content": decoded}
        event_type = str(payload.get("type", "event"))
    return pb.RunEvent(
        event_seq=sequence,
        run_id=request_id,
        type=event_type,
        payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        created_at=datetime.now(UTC).isoformat(),
    )


def _is_done_event(chunk: str) -> bool:
    return any(line == "data: [DONE]" for line in chunk.splitlines())


async def _wait_for_terminal_run(
    runtime: Any,
    request_id: str,
    principal_id: str,
    *,
    timeout_seconds: float = 2.0,
) -> Any | None:
    try:
        async with asyncio.timeout(timeout_seconds):
            while True:
                record = await runtime.run_registry.get(
                    request_id, principal_id
                )
                if record.is_terminal:
                    return record
                await asyncio.sleep(0.05)
    except TimeoutError:
        return None


class AgentRuntimeGrpcService(pb_grpc.AgentRuntimeServiceServicer):
    def __init__(self, runtime: Any | None = None) -> None:
        if runtime is None:
            # Production keeps the existing singleton runtime. Import it only
            # when needed so injected transport tests and dedicated workers do
            # not initialize the complete LangGraph application as a side effect.
            from service import service as default_runtime

            runtime = default_runtime
        self._runtime = runtime

    async def StartRun(self, request: pb.StartRunRequest, context: Any) -> pb.Run:
        claims = await _authenticate(
            request, context, "StartRun", request.request_id, request.identity
        )
        try:
            runtime_input = StreamInput(
                message=request.message,
                model=request.model if request.HasField("model") else None,
                thread_id=request.thread_id,
                user_id=claims.subject,
                request_id=request.request_id,
                timeout_seconds=(
                    request.timeout_seconds if request.HasField("timeout_seconds") else None
                ),
                agent_config=_config(request.config_json),
                stream_tokens=request.stream_tokens,
            )
            runtime_input.bind_runtime_identity(
                principal_id=claims.subject,
                tenant_id=claims.tenant_id,
                project_id=claims.project_id,
            )
            # ``stream`` owns validation, idempotent start and producer creation.
            # The gRPC subscription attaches separately to the same registry run.
            await self._runtime.stream(runtime_input, _AGENT_ID)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except HTTPException as exc:
            await _abort_http(context, exc)
        return await _status_or_abort(
            self._runtime, request.request_id, _owner_id(claims), context
        )

    async def GetRun(self, request: pb.GetRunRequest, context: Any) -> pb.Run:
        claims = await _authenticate(
            request, context, "GetRun", request.request_id, request.identity
        )
        return await _status_or_abort(
            self._runtime, request.request_id, _owner_id(claims), context
        )

    async def ControlRun(self, request: pb.ControlRunRequest, context: Any) -> pb.Run:
        claims = await _authenticate(
            request,
            context,
            "ControlRun",
            request.run.request_id,
            request.run.identity,
        )
        if request.control != pb.RUN_CONTROL_CANCEL:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, "Unsupported run control.")
        try:
            await self._runtime.cancel_run(request.run.request_id, _owner_id(claims))
        except HTTPException as exc:
            await _abort_http(context, exc)
        except RPCError as exc:
            code = (
                grpc.StatusCode.UNAVAILABLE
                if exc.status == RPCStatusCode.UNAVAILABLE
                else grpc.StatusCode.INTERNAL
            )
            await _abort(context, code, "Temporal cancellation signal failed.")
        return await _status_or_abort(
            self._runtime, request.run.request_id, _owner_id(claims), context
        )

    async def ResumeRun(self, request: pb.ResumeRunRequest, context: Any) -> pb.Run:
        claims = await _authenticate(
            request,
            context,
            "ResumeRun",
            request.run.request_id,
            request.run.identity,
        )
        try:
            if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", request.interaction_id):
                raise ValueError("interaction_id is invalid")
            response_request_id = str(UUID(request.response_request_id))
            if request.state_version < 1:
                raise ValueError("state_version must be positive")
            current = await self._runtime.run_status(
                request.run.request_id, _owner_id(claims)
            )
            runtime_input = StreamInput(
                message=request.answer,
                model=request.model if request.HasField("model") else None,
                thread_id=current.thread_id,
                user_id=claims.subject,
                request_id=request.run.request_id,
                timeout_seconds=(
                    request.timeout_seconds
                    if request.HasField("timeout_seconds")
                    else None
                ),
                agent_config=_config(request.config_json),
                stream_tokens=True,
            )
            runtime_input.bind_runtime_identity(
                principal_id=claims.subject,
                tenant_id=claims.tenant_id,
                project_id=claims.project_id,
            )
            status = await self._runtime.resume_interaction(
                runtime_input,
                interaction_id=request.interaction_id,
                response_request_id=response_request_id,
                state_version=int(request.state_version),
                agent_id=_AGENT_ID,
            )
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            await _abort(context, grpc.StatusCode.INVALID_ARGUMENT, str(exc))
        except HTTPException as exc:
            await _abort_http(context, exc)
        return _run_message(status)

    async def SubscribeRunEvents(
        self,
        request: pb.SubscribeRunEventsRequest,
        context: Any,
    ):
        claims = await _authenticate(
            request,
            context,
            "SubscribeRunEvents",
            request.run.request_id,
            request.run.identity,
        )
        try:
            record = await self._runtime.run_registry.get(
                request.run.request_id, _owner_id(claims)
            )
        except RunNotFoundError:
            await _abort(context, grpc.StatusCode.NOT_FOUND, "Run not found.")
        except RunAccessError:
            await _abort(context, grpc.StatusCode.PERMISSION_DENIED, "Run access denied.")
        async for chunk in self._runtime.run_registry.subscribe(
            record,
            last_event_id=int(request.last_event_seq),
        ):
            terminal = None
            if _is_done_event(chunk):
                try:
                    terminal = await _wait_for_terminal_run(
                        self._runtime,
                        request.run.request_id,
                        _owner_id(claims),
                    )
                except (RunNotFoundError, RunAccessError):
                    await _abort(
                        context,
                        grpc.StatusCode.INTERNAL,
                        "Run disappeared while resolving its terminal event.",
                    )
                if terminal is None:
                    await _abort(
                        context,
                        grpc.StatusCode.UNAVAILABLE,
                        "Run did not reach a verified terminal state in time.",
                    )
            event = _event_message(
                chunk,
                request.run.request_id,
                terminal=terminal,
            )
            if event is not None:
                yield event


async def start_grpc_server(host: str, port: int) -> grpc.aio.Server:
    secret = settings.RATSNEST_INTERNAL_SIGNING_SECRET
    if secret is None or len(secret.get_secret_value().encode("utf-8")) < 32:
        raise RuntimeError("Internal gRPC requires RATSNEST_INTERNAL_SIGNING_SECRET.")
    server = grpc.aio.server(
        options=(
            ("grpc.max_receive_message_length", settings.MAX_REQUEST_BODY_BYTES),
            ("grpc.max_send_message_length", settings.SSE_MAX_EVENT_BYTES),
        )
    )
    pb_grpc.add_AgentRuntimeServiceServicer_to_server(AgentRuntimeGrpcService(), server)
    address = f"{host}:{port}"
    if server.add_insecure_port(address) == 0:
        raise RuntimeError(f"Unable to bind internal gRPC server to {address}")
    await server.start()
    return server


__all__ = ["AgentRuntimeGrpcService", "start_grpc_server"]
