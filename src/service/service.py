import asyncio
import hashlib
import hmac
import inspect
import json
import logging
import warnings
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from time import monotonic
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from langchain_core._api import LangChainBetaWarning
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    AnyMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langfuse import Langfuse  # type: ignore[import-untyped]
from langfuse.langchain import (
    CallbackHandler,  # type: ignore[import-untyped]
)
from langgraph.types import Command, Interrupt
from langsmith import Client as LangsmithClient
from langsmith import uuid7

from agents import DEFAULT_AGENT, AgentGraph, get_agent, get_all_agent_info, load_agent
from core import settings
from core.settings import RunRegistryBackend
from memory import initialize_database, initialize_store
from schema import (
    ChatHistory,
    ChatHistoryInput,
    ChatMessage,
    Feedback,
    FeedbackResponse,
    RunCancelResponse,
    RunStatus,
    ServiceMetadata,
    StreamInput,
    UserInput,
)
from service.internal_api import router as internal_router
from service.redis_run_registry import RedisRunRegistry, RunHandle
from service.run_coordination import checkpoint_thread_candidates, serialize_thread_run
from service.run_registry import (
    RunAccessError,
    RunConflictError,
    RunNotFoundError,
    RunOverloadedError,
    RunRecord,
    RunRegistry,
)
from service.runtime_identity import effective_user_id, execution_scope
from service.utils import (
    convert_message_content_to_string,
    execution_error_payload,
    explicit_reasoning_content,
    langchain_to_chat_message,
    remove_tool_calls,
    try_stream_chat_message,
    visible_stream_messages,
)

warnings.filterwarnings("ignore", category=LangChainBetaWarning)
logger = logging.getLogger(__name__)

RunRecordLike = RunRecord | RunHandle


def _create_run_registry() -> RunRegistry | RedisRunRegistry:
    common: dict[str, Any] = {
        "max_concurrent": settings.MAX_CONCURRENT_RUNS,
        "max_queued": settings.MAX_QUEUED_RUNS,
        "default_timeout": settings.RUN_TIMEOUT_SECONDS,
        "heartbeat_seconds": settings.SSE_HEARTBEAT_SECONDS,
        "event_buffer_size": settings.SSE_EVENT_BUFFER_SIZE,
        "max_event_bytes": settings.SSE_MAX_EVENT_BYTES,
        "retention_seconds": settings.RUN_RETENTION_SECONDS,
    }
    if settings.RUN_REGISTRY_BACKEND == RunRegistryBackend.MEMORY:
        return RunRegistry(**common)
    if settings.REDIS_URL is None:
        raise RuntimeError("REDIS_URL is required when RUN_REGISTRY_BACKEND=redis")
    return RedisRunRegistry(
        **common,
        redis_url=settings.REDIS_URL.get_secret_value(),
        key_prefix=settings.REDIS_KEY_PREFIX,
        lease_seconds=settings.REDIS_RUN_LEASE_SECONDS,
        stream_block_ms=settings.REDIS_STREAM_BLOCK_MS,
        audit_outbox_maxlen=settings.REDIS_AUDIT_OUTBOX_MAXLEN,
    )


run_registry = _create_run_registry()
_MAX_REQUEST_BODY_BYTES = settings.MAX_REQUEST_BODY_BYTES


def _ensure_thread_id(user_input: UserInput) -> str:
    if user_input.thread_id is None:
        user_input.thread_id = str(uuid4())
    return user_input.thread_id


def _ensure_request_id(user_input: UserInput) -> str:
    if user_input.request_id is None:
        user_input.request_id = str(uuid4())
    return user_input.request_id


def _request_fingerprint(user_input: UserInput, agent_id: str) -> str:
    payload = user_input.model_dump(
        mode="json",
        exclude={"request_id", "last_event_id"},
    )
    payload["agent_id"] = agent_id
    if scope := execution_scope(user_input):
        payload["runtime_owner"] = scope.owner_id
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _run_registry_error(exc: Exception) -> HTTPException:
    if isinstance(exc, RunConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, RunOverloadedError):
        return HTTPException(
            status_code=429,
            detail=str(exc),
            headers={"Retry-After": "5"},
        )
    if isinstance(exc, RunAccessError):
        return HTTPException(status_code=403, detail="Run does not belong to this user.")
    if isinstance(exc, RunNotFoundError):
        return HTTPException(status_code=404, detail="Run not found.")
    return HTTPException(status_code=500, detail="Unexpected run registry error.")


def _raise_run_failure(record: RunRecordLike) -> None:
    if record.status in {"failed", "cancelled", "timed_out"}:
        if record.error_code == "request_rejected":
            raise HTTPException(
                status_code=record.http_status,
                detail=record.error or "Request rejected.",
            )
        raise HTTPException(
            status_code=record.http_status,
            detail={
                "code": record.error_code,
                "message": record.error or "Run failed.",
                "request_id": record.request_id,
            },
        )


def _get_agent_or_404(agent_id: str) -> AgentGraph:
    try:
        return get_agent(agent_id)
    except (KeyError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Agent '{agent_id}' is not available.",
        ) from exc


def _state_scope(state: Any) -> dict[str, Any]:
    """Read ownership/idempotency fields from legacy config or saver metadata."""
    scope: dict[str, Any] = {}
    if state.config:
        configurable = state.config.get("configurable", {})
        if isinstance(configurable, dict):
            scope.update(configurable)
    if isinstance(state.metadata, dict):
        scope.update(state.metadata)
    return scope


def custom_generate_unique_id(route: APIRoute) -> str:
    """Generate idiomatic operation IDs for OpenAPI client generation."""
    return route.name


def verify_bearer(
    http_auth: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(HTTPBearer(description="Please provide AUTH_SECRET api key.", auto_error=False)),
    ],
) -> None:
    if not settings.AUTH_SECRET:
        return
    auth_secret = settings.AUTH_SECRET.get_secret_value()
    if not http_auth or not hmac.compare_digest(http_auth.credentials, auth_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Configurable lifespan that initializes the appropriate database checkpointer, store,
    and agents with async loading - for example for starting up MCP clients.
    """
    app.state.ready = False
    app.state.failed_agents = []
    grpc_server = None
    try:
        if (
            settings.is_production()
            and settings.REQUIRE_AUTH_IN_PRODUCTION
            and not settings.AUTH_SECRET
        ):
            raise RuntimeError(
                "AUTH_SECRET is required when MODE=production. "
                "Set REQUIRE_AUTH_IN_PRODUCTION=false only behind a trusted auth proxy."
            )
        await run_registry.startup()
        # Initialize both checkpointer (for short-term memory) and store (for long-term memory)
        async with initialize_database() as saver, initialize_store() as store:
            # Set up both components
            if hasattr(saver, "setup"):  # ignore: union-attr
                await saver.setup()
            # Only setup store for Postgres as InMemoryStore doesn't need setup
            if hasattr(store, "setup"):  # ignore: union-attr
                await store.setup()

            if not settings.AUTH_SECRET:
                logger.warning(
                    "AUTH_SECRET is not configured — all API endpoints are unauthenticated. "
                    "Set AUTH_SECRET in your environment to enable bearer token authentication."
                )

            # Configure agents with both memory components and async loading
            agents = get_all_agent_info()
            for a in agents:
                try:
                    await load_agent(a.key)
                except Exception:
                    app.state.failed_agents.append(a.key)
                    logger.exception("Failed to load agent %s", a.key)
                try:
                    agent = get_agent(a.key)
                except Exception:
                    logger.debug(
                        "Agent graph unavailable after load failure: %s",
                        a.key,
                        exc_info=True,
                    )
                    continue
                # Set checkpointer for thread-scoped memory (conversation history)
                agent.checkpointer = saver
                # Set store for long-term memory (cross-conversation knowledge)
                agent.store = store
                if a.key not in app.state.failed_agents:
                    logger.info("Agent loaded: %s", a.key)
                    # Continue with other agents rather than failing startup
            app.state.ready = not app.state.failed_agents
            if settings.RATSNEST_INTERNAL_GRPC_ENABLED:
                from service.grpc_runtime import start_grpc_server

                grpc_server = await start_grpc_server(
                    settings.RATSNEST_INTERNAL_GRPC_HOST,
                    settings.RATSNEST_INTERNAL_GRPC_PORT,
                )
                app.state.grpc_server = grpc_server
            yield
    except Exception:
        logger.exception("Error during database/store/agents initialization")
        raise
    finally:
        app.state.ready = False
        if grpc_server is not None:
            await grpc_server.stop(settings.RATSNEST_INTERNAL_GRPC_SHUTDOWN_SECONDS)
        await run_registry.shutdown()


app = FastAPI(lifespan=lifespan, generate_unique_id_function=custom_generate_unique_id)
router = APIRouter(dependencies=[Depends(verify_bearer)])


@app.middleware("http")
async def request_guard(request: Request, call_next):
    """Bound request memory and attach a correlation ID to every response."""
    correlation_id = request.headers.get("X-Request-ID", "")
    if not correlation_id or len(correlation_id) > 200:
        correlation_id = str(uuid4())
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            too_large = int(content_length) > _MAX_REQUEST_BODY_BYTES
        except ValueError:
            too_large = True
        if too_large:
            return JSONResponse(
                status_code=413,
                content={"detail": "Request body is too large."},
                headers={"X-Request-ID": correlation_id},
            )

    started = monotonic()
    response = await call_next(request)
    response.headers["X-Request-ID"] = correlation_id
    logger.info(
        "request method=%s path=%s status=%s duration_ms=%.1f request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        (monotonic() - started) * 1_000,
        correlation_id,
    )
    return response


@router.get("/info")
async def info() -> ServiceMetadata:
    models = list(settings.AVAILABLE_MODELS)
    models.sort()
    return ServiceMetadata(
        agents=get_all_agent_info(),
        models=models,
        default_agent=DEFAULT_AGENT,
        default_model=settings.DEFAULT_MODEL,
    )


async def _checkpoint_thread_id(
    agent: AgentGraph,
    agent_id: str,
    user_id: str,
    client_thread_id: str,
    *,
    allow_legacy: bool,
) -> str:
    """Use an owned legacy checkpoint when available; otherwise use the scoped key."""
    candidates = checkpoint_thread_candidates(
        agent_id,
        user_id,
        client_thread_id,
        allow_legacy=allow_legacy,
    )
    for thread_id in candidates:
        state = await agent.aget_state(config=RunnableConfig(configurable={"thread_id": thread_id}))
        if state.values and _state_scope(state).get("user_id") == user_id:
            return thread_id
    return candidates[0]


async def _handle_input(
    user_input: UserInput,
    agent: AgentGraph,
    agent_id: str = DEFAULT_AGENT,
) -> tuple[dict[str, Any], UUID]:
    """
    Parse user input and handle any required interrupt resumption.
    Returns kwargs for agent invocation and the run_id.
    """
    _ensure_request_id(user_input)
    run_id = uuid7()
    client_thread_id = user_input.thread_id or str(uuid4())
    runtime_scope = execution_scope(user_input)
    user_id = effective_user_id(user_input, user_input.user_id) or str(uuid4())
    thread_id = await _checkpoint_thread_id(
        agent,
        agent_id,
        user_id,
        client_thread_id,
        allow_legacy=runtime_scope is None,
    )

    configurable = {
        "thread_id": thread_id,
        "client_thread_id": client_thread_id,
        "user_id": user_id,
        "request_id": user_input.request_id,
        "request_fingerprint": _request_fingerprint(user_input, agent_id),
    }
    if runtime_scope is not None:
        configurable.update(
            {
                "principal_scope": runtime_scope.principal,
                "tenant_scope": runtime_scope.tenant,
                "project_scope": runtime_scope.project,
            }
        )
    if user_input.model is not None:
        if user_input.model not in settings.AVAILABLE_MODELS:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{user_input.model}' is not available. "
                f"Allowed: {[m.value for m in settings.AVAILABLE_MODELS]}",
            )
        configurable["model"] = user_input.model

    callbacks: list[Any] = []
    if settings.LANGFUSE_TRACING:
        # Initialize Langfuse CallbackHandler for Langchain (tracing)
        langfuse_handler = CallbackHandler()

        callbacks.append(langfuse_handler)

    if user_input.agent_config:
        # Check for reserved keys (including 'model' even if not in configurable)
        reserved_keys = {
            "thread_id",
            "client_thread_id",
            "user_id",
            "request_id",
            "request_fingerprint",
            "principal_scope",
            "tenant_scope",
            "project_scope",
            "model",
        }
        if overlap := reserved_keys & user_input.agent_config.keys():
            raise HTTPException(
                status_code=422,
                detail=f"agent_config contains reserved keys: {overlap}",
            )
        configurable.update(user_input.agent_config)

    config = RunnableConfig(
        configurable=configurable,
        run_id=run_id,
        callbacks=callbacks,
    )

    # Check for interrupts that need to be resumed
    state = await agent.aget_state(config=config)

    # Validate that the caller owns this thread
    if state.values:
        stored_user_id = _state_scope(state).get("user_id")
        if stored_user_id and stored_user_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="thread_id does not belong to the provided user_id",
            )

    stored_scope = _state_scope(state)
    stored_request_id = stored_scope.get("request_id")
    stored_fingerprint = stored_scope.get("request_fingerprint")
    if (
        stored_request_id == user_input.request_id
        and stored_fingerprint
        and stored_fingerprint != configurable["request_fingerprint"]
    ):
        raise HTTPException(
            status_code=409,
            detail="request_id was already used for a different request.",
        )

    interrupted_tasks = [
        task for task in state.tasks if hasattr(task, "interrupts") and task.interrupts
    ]

    input: Command | dict[str, Any] | None
    if interrupted_tasks:
        # assume user input is response to resume agent execution from interrupt
        input = Command(resume=user_input.message)
    elif stored_request_id == user_input.request_id and state.values:
        # The process may have restarted after checkpointing this request. Resume
        # the existing graph state rather than appending the same human message.
        input = None
    else:
        input = {"messages": [HumanMessage(content=user_input.message)]}

    kwargs = {
        "input": input,
        "config": config,
    }
    if input is None and not state.next and state.values.get("messages"):
        kwargs["_checkpoint_message"] = state.values["messages"][-1]

    return kwargs, run_id


async def _invoke_unlocked(
    user_input: UserInput,
    agent: AgentGraph,
    agent_id: str,
    record: RunRecordLike,
) -> ChatMessage:
    """Execute one invocation after the registry owns its lifecycle."""
    kwargs, run_id = await _handle_input(user_input, agent, agent_id)
    record.run_id = str(run_id)
    checkpoint_message = kwargs.pop("_checkpoint_message", None)
    if checkpoint_message is not None:
        output = langchain_to_chat_message(checkpoint_message)
        output.run_id = str(run_id)
        return output
    response_events: list[tuple[str, Any]] = await agent.ainvoke(  # type: ignore[assignment]
        **kwargs,
        stream_mode=["updates", "values"],
    )
    if not response_events:
        raise RuntimeError("Agent returned no response events.")
    response_type, response = response_events[-1]
    if response_type == "values":
        output = langchain_to_chat_message(response["messages"][-1])
    elif response_type == "updates" and "__interrupt__" in response:
        output = langchain_to_chat_message(AIMessage(content=response["__interrupt__"][0].value))
    else:
        raise ValueError(f"Unexpected response type: {response_type}")
    output.run_id = str(run_id)
    return output


@router.post("/{agent_id}/invoke", operation_id="invoke_with_agent_id")
@router.post("/invoke")
async def invoke(user_input: UserInput, agent_id: str = DEFAULT_AGENT) -> ChatMessage:
    """Idempotently invoke an agent and return its final response."""
    agent = _get_agent_or_404(agent_id)
    thread_id = _ensure_thread_id(user_input)
    request_id = _ensure_request_id(user_input)
    owner_id = effective_user_id(user_input, user_input.user_id)

    async def producer(record: RunRecordLike) -> ChatMessage:
        async with serialize_thread_run(
            agent_id,
            thread_id,
            user_id=owner_id,
        ):
            return await _invoke_unlocked(user_input, agent, agent_id, record)

    try:
        record, _ = await run_registry.start(
            request_id=request_id,
            fingerprint=_request_fingerprint(user_input, agent_id),
            kind="invoke",
            agent_id=agent_id,
            thread_id=thread_id,
            user_id=owner_id,
            timeout_seconds=user_input.timeout_seconds,
            producer=producer,
        )
    except (RunConflictError, RunOverloadedError, RunAccessError) as exc:
        raise _run_registry_error(exc) from exc

    record = await run_registry.wait_terminal(record)
    _raise_run_failure(record)
    if isinstance(record.result, dict):
        record.result = ChatMessage.model_validate(record.result)
    if not isinstance(record.result, ChatMessage):
        raise HTTPException(status_code=500, detail="Agent returned an invalid response.")
    return record.result


async def _message_generator_unlocked(
    user_input: StreamInput, agent_id: str = DEFAULT_AGENT
) -> AsyncGenerator[str, None]:
    """
    Generate a stream of messages from the agent.

    This is the workhorse method for the /stream endpoint.
    """
    agent = _get_agent_or_404(agent_id)
    kwargs, run_id = await _handle_input(user_input, agent, agent_id)
    checkpoint_message = kwargs.pop("_checkpoint_message", None)
    if checkpoint_message is not None:
        chat_message = langchain_to_chat_message(checkpoint_message)
        chat_message.run_id = str(run_id)
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "message",
                    "content": chat_message.model_dump(),
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"
        return

    try:
        # Process streamed events from the graph and yield messages over the SSE stream.
        async for stream_event in agent.astream(  # type: ignore[no-matching-overload]
            **kwargs, stream_mode=["updates", "messages", "custom"], subgraphs=True
        ):
            if not isinstance(stream_event, tuple):
                continue
            # Handle different stream event structures based on subgraphs
            if len(stream_event) == 3:
                # With subgraphs=True: (node_path, stream_mode, event)
                _, stream_mode, event = stream_event
            else:
                # Without subgraphs: (stream_mode, event)
                stream_mode, event = stream_event
            new_messages: list[Any] = []
            if stream_mode == "updates":
                for node, updates in event.items():
                    # A simple approach to handle agent interrupts.
                    # In a more sophisticated implementation, we could add
                    # some structured ChatMessage type to return the interrupt value.
                    if node == "__interrupt__":
                        interrupt: Interrupt
                        for interrupt in updates:
                            new_messages.append(AIMessage(content=interrupt.value))
                        continue
                    updates = updates or {}
                    update_messages = updates.get("messages", [])
                    # Preserve the final handoff message from nested subgraphs.
                    if ("supervisor" in node or "sub-agent" in node) and update_messages:
                        # the only tools that come from the actual agent are the handoff and handback tools
                        if isinstance(update_messages[-1], ToolMessage):
                            if "sub-agent" in node and len(update_messages) > 1:
                                # If this is a sub-agent, we want to keep the last 2 messages - the handback tool, and it's result
                                update_messages = update_messages[-2:]
                            else:
                                # If this is a supervisor, we want to keep the last message only - the handoff result. The tool comes from the 'agent' node.
                                update_messages = [update_messages[-1]]
                        else:
                            update_messages = []
                    new_messages.extend(update_messages)

            if stream_mode == "custom":
                # LangGraph custom writers are an application event channel, not
                # LangChain messages. Preserve every structured event as custom
                # SSE data so new harness event kinds do not fall into message
                # parsing and break the stream.
                if isinstance(event, dict):
                    if event.get("kind") == "artifact_manifest":
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "artifact_manifest",
                                    "content": event,
                                }
                            )
                            + "\n\n"
                        )
                        continue
                    chat_message = ChatMessage(
                        type="custom",
                        content="",
                        custom_data=event,
                        run_id=str(run_id),
                    )
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "type": "message",
                                "content": chat_message.model_dump(),
                            }
                        )
                        + "\n\n"
                    )
                    continue
                new_messages = [event]

            # LangGraph streaming may emit tuples: (field_name, field_value)
            # e.g. ('content', <str>), ('tool_calls', [ToolCall,...]), ('additional_kwargs', {...}), etc.
            # We accumulate only supported fields into `parts` and skip unsupported metadata.
            # More info at: https://langchain-ai.github.io/langgraph/cloud/how-tos/stream_messages/
            processed_messages = []
            current_message: dict[str, Any] = {}
            for message in new_messages:
                if isinstance(message, tuple):
                    key, value = message
                    # Store parts in temporary dict
                    current_message[key] = value
                else:
                    # Add complete message if we have one in progress
                    if current_message:
                        processed_messages.append(_create_ai_message(current_message))
                        current_message = {}
                    processed_messages.append(message)

            # Add any remaining message parts
            if current_message:
                processed_messages.append(_create_ai_message(current_message))

            for message in visible_stream_messages(processed_messages):
                chat_message, adaptation_error = try_stream_chat_message(message)
                if chat_message is None:
                    logger.warning(
                        "Skipping non-displayable stream message %s: %s",
                        type(message).__name__,
                        adaptation_error,
                    )
                    continue
                chat_message.run_id = str(run_id)
                # The client already renders the submitted user message. LangGraph
                # may replay current or historical human messages in node updates;
                # none of them are agent output for the live response stream.
                if chat_message.type == "human":
                    continue
                yield f"data: {json.dumps({'type': 'message', 'content': chat_message.model_dump()})}\n\n"

            if stream_mode == "messages":
                if not user_input.stream_tokens:
                    continue
                msg, metadata = event
                if "skip_stream" in metadata.get("tags", []):
                    continue
                # For some reason, astream("messages") causes non-LLM nodes to send extra messages.
                # Drop them.
                if not isinstance(msg, AIMessageChunk):
                    continue
                reasoning = explicit_reasoning_content(msg)
                if reasoning:
                    yield f"data: {json.dumps({'type': 'reasoning', 'content': reasoning})}\n\n"
                content = remove_tool_calls(msg.content)
                if content:
                    # Empty content in the context of OpenAI usually means
                    # that the model is asking for a tool to be invoked.
                    # So we only print non-empty content.
                    yield f"data: {json.dumps({'type': 'token', 'content': convert_message_content_to_string(content)})}\n\n"
    except Exception as e:
        logger.exception("Agent execution stream failed")
        yield f"data: {json.dumps(execution_error_payload(e))}\n\n"
    finally:
        yield "data: [DONE]\n\n"


async def message_generator(
    user_input: StreamInput,
    agent_id: str = DEFAULT_AGENT,
) -> AsyncGenerator[str, None]:
    """Serialize one thread while forwarding its SSE stream."""
    thread_id = _ensure_thread_id(user_input)
    owner_id = effective_user_id(user_input, user_input.user_id)
    async with serialize_thread_run(
        agent_id,
        thread_id,
        user_id=owner_id,
    ):
        async for event in _message_generator_unlocked(user_input, agent_id):
            yield event


def _create_ai_message(parts: dict) -> AIMessage:
    sig = inspect.signature(AIMessage)
    valid_keys = set(sig.parameters)
    filtered = {k: v for k, v in parts.items() if k in valid_keys}
    return AIMessage(**filtered)


def _sse_response_example() -> dict[int | str, Any]:
    return {
        status.HTTP_200_OK: {
            "description": "Server Sent Event Response",
            "content": {
                "text/event-stream": {
                    "example": "data: {'type': 'token', 'content': 'Hello'}\n\ndata: {'type': 'token', 'content': ' World'}\n\ndata: [DONE]\n\n",
                    "schema": {"type": "string"},
                }
            },
        }
    }


@router.post(
    "/{agent_id}/stream",
    response_class=StreamingResponse,
    responses=_sse_response_example(),
    operation_id="stream_with_agent_id",
)
@router.post("/stream", response_class=StreamingResponse, responses=_sse_response_example())
async def stream(user_input: StreamInput, agent_id: str = DEFAULT_AGENT) -> StreamingResponse:
    """
    Stream an agent's response to a user input, including intermediate messages and tokens.

    If agent_id is not provided, the default agent will be used.
    Use thread_id to persist and continue a multi-turn conversation. run_id kwarg
    is also attached to all messages for recording feedback.
    Use user_id to persist and continue a conversation across multiple threads.

    Set `stream_tokens=false` to return intermediate messages but not token-by-token.
    """
    _get_agent_or_404(agent_id)
    thread_id = _ensure_thread_id(user_input)
    request_id = _ensure_request_id(user_input)
    owner_id = effective_user_id(user_input, user_input.user_id)

    async def producer(record: RunRecordLike) -> dict[str, Any]:
        result: dict[str, Any] = {}
        async for event in message_generator(user_input, agent_id):
            await run_registry.set_run_id(record, _event_run_id(event))
            manifest = _event_artifact_manifest(event)
            if manifest is not None:
                result = {
                    "artifact_manifest": manifest,
                    "delivery_status": manifest.get("delivery_status"),
                }
            if _is_error_event(event):
                await run_registry.mark_stream_failed(
                    record,
                    code="agent_stream_error",
                    message="The agent stream reported an error.",
                )
            await run_registry.append_event(record, event)
        return result

    try:
        record, _ = await run_registry.start(
            request_id=request_id,
            fingerprint=_request_fingerprint(user_input, agent_id),
            kind="stream",
            agent_id=agent_id,
            thread_id=thread_id,
            user_id=owner_id,
            timeout_seconds=user_input.timeout_seconds,
            producer=producer,
        )
    except (RunConflictError, RunOverloadedError, RunAccessError) as exc:
        raise _run_registry_error(exc) from exc

    return StreamingResponse(
        run_registry.subscribe(record, last_event_id=user_input.last_event_id),
        media_type="text/event-stream",
        headers={
            "X-Request-ID": request_id,
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


def _is_error_event(event: str) -> bool:
    for line in event.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[6:]
        if raw == "[DONE]":
            return False
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return False
        return payload.get("type") == "error"
    return False


def _event_run_id(event: str) -> str | None:
    for line in event.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "message":
            continue
        content = payload.get("content")
        if isinstance(content, dict) and isinstance(content.get("run_id"), str):
            return content["run_id"]
    return None


def _event_artifact_manifest(event: str) -> dict[str, Any] | None:
    """Extract only the safe, storage-facing manifest from an SSE envelope."""

    for line in event.splitlines():
        if not line.startswith("data: ") or line == "data: [DONE]":
            continue
        try:
            payload = json.loads(line[6:])
        except json.JSONDecodeError:
            continue
        content = payload.get("content")
        if payload.get("type") == "artifact_manifest" and isinstance(content, dict):
            return content
    return None


@router.get("/runs/{request_id}")
async def run_status(
    request_id: str,
    user_id: Annotated[str | None, Query(max_length=200)] = None,
) -> RunStatus:
    try:
        record = await run_registry.get(request_id, user_id)
    except (RunNotFoundError, RunAccessError) as exc:
        raise _run_registry_error(exc) from exc
    return RunStatus.model_validate(record.public_dict())


@router.delete("/runs/{request_id}")
async def cancel_run(
    request_id: str,
    user_id: Annotated[str | None, Query(max_length=200)] = None,
) -> RunCancelResponse:
    try:
        current = await run_registry.get(request_id, user_id)
        if current.agent_id == "ratsnestpro-multi-agent" and not current.is_terminal:
            from agents.ratsnestpro.temporal.client import (
                signal_hardware_workflow_by_request_id,
                temporal_enabled,
            )

            if temporal_enabled():
                await signal_hardware_workflow_by_request_id(request_id, "cancel")
        record = await run_registry.cancel(request_id, user_id)
    except (RunNotFoundError, RunAccessError) as exc:
        raise _run_registry_error(exc) from exc
    return RunCancelResponse(request_id=request_id, status=record.status)


@router.post("/feedback")
async def feedback(feedback: Feedback) -> FeedbackResponse:
    """
    Record feedback for a run to LangSmith.

    This is a simple wrapper for the LangSmith create_feedback API, so the
    credentials can be stored and managed in the service rather than the client.
    See: https://api.smith.langchain.com/redoc#tag/feedback/operation/create_feedback_api_v1_feedback_post
    """
    client = LangsmithClient()
    kwargs = feedback.kwargs or {}
    client.create_feedback(
        run_id=feedback.run_id,
        key=feedback.key,
        score=feedback.score,
        **kwargs,
    )
    return FeedbackResponse()


@router.post("/{agent_id}/history", operation_id="history_with_agent_id")
@router.post("/history")
async def history(input: ChatHistoryInput, agent_id: str = DEFAULT_AGENT) -> ChatHistory:
    """
    Get chat history for a thread and agent.

    If agent_id is not provided, the default agent will be used.
    """
    agent = _get_agent_or_404(agent_id)
    try:
        owner_id = effective_user_id(input, input.user_id)
        runtime_scope = execution_scope(input)
        if owner_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="thread_id does not belong to the provided user_id",
            )

        state_snapshot = None
        for thread_id in checkpoint_thread_candidates(
            agent_id,
            owner_id,
            input.thread_id,
            allow_legacy=runtime_scope is None,
        ):
            candidate = await agent.aget_state(
                config=RunnableConfig(configurable={"thread_id": thread_id})
            )
            if candidate.values and _state_scope(candidate).get("user_id") == owner_id:
                state_snapshot = candidate
                break

        if state_snapshot is None:
            return ChatHistory(messages=[])

        messages: list[AnyMessage] = state_snapshot.values.get("messages", [])
        chat_messages: list[ChatMessage] = [langchain_to_chat_message(m) for m in messages]
        return ChatHistory(messages=chat_messages)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"An exception occurred: {e}")
        raise HTTPException(status_code=500, detail="Unexpected error")


@app.get("/health/live")
async def liveness_check() -> dict[str, str]:
    """Process liveness; it deliberately does not probe dependencies."""
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness_check() -> dict[str, Any]:
    """Readiness reflects initialization, mandatory agents, and live run storage."""
    if not getattr(app.state, "ready", False):
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "failed_agents": getattr(app.state, "failed_agents", []),
            },
        )
    try:
        async with asyncio.timeout(1):
            run_storage = await run_registry.healthcheck()
    except Exception:
        raise HTTPException(
            status_code=503,
            detail={"status": "not_ready", "dependency": "run_registry"},
        ) from None
    return {"status": "ready", "failed_agents": [], "run_storage": run_storage}


@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Backward-compatible aggregate health endpoint."""
    health_status: dict[str, Any] = {
        "status": "ok" if getattr(app.state, "ready", False) else "starting",
        "ready": getattr(app.state, "ready", False),
    }
    if settings.LANGFUSE_TRACING:
        try:
            langfuse = Langfuse()
            health_status["langfuse"] = "connected" if langfuse.auth_check() else "disconnected"
        except Exception as e:
            logger.error(f"Langfuse connection error: {e}")
            health_status["langfuse"] = "disconnected"

    return health_status


@router.get("/metrics")
async def runtime_metrics() -> dict[str, Any]:
    """Small dependency-free runtime snapshot for operations and alerting."""
    return {
        "runs": await run_registry.metrics(),
        "ready": getattr(app.state, "ready", False),
        "failed_agents": getattr(app.state, "failed_agents", []),
    }


app.include_router(internal_router)
app.include_router(router)
