from datetime import datetime
from typing import Any, Literal, NotRequired

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SerializeAsAny
from typing_extensions import TypedDict

from schema.models import AllModelEnum, AnthropicModelName, OpenAIModelName


class AgentInfo(BaseModel):
    """Info about an available agent."""

    key: str = Field(
        description="Agent key.",
        examples=["research-assistant"],
    )
    description: str = Field(
        description="Description of the agent.",
        examples=["A research assistant for generating research papers."],
    )


class CapabilityProfileInfo(BaseModel):
    """Immutable capability profile metadata exposed by the agent runtime."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,63}$")
    version: str = Field(pattern=r"^\d+\.\d+(?:\.\d+)?$")
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    title: str
    description: str


class ServiceMetadata(BaseModel):
    """Metadata about the service including available agents and models."""

    agents: list[AgentInfo] = Field(
        description="List of available agents.",
    )
    models: list[AllModelEnum] = Field(
        description="List of available LLMs.",
    )
    default_agent: str = Field(
        description="Default agent used when none is specified.",
        examples=["research-assistant"],
    )
    default_model: AllModelEnum = Field(
        description="Default model used when none is specified.",
    )
    profiles: list[CapabilityProfileInfo] = Field(
        default_factory=list,
        description="Versioned production capabilities available to RatsNestPro.",
    )


class _RuntimeIdentityBoundModel(BaseModel):
    """Request model with an internal-only, transport-verified identity binding.

    The private attribute is absent from validation, serialization and OpenAPI.
    Only the signed internal adapters bind it after authenticating the request.
    """

    model_config = ConfigDict(extra="forbid")

    _runtime_identity: tuple[str, str, str] | None = PrivateAttr(default=None)

    def bind_runtime_identity(
        self,
        *,
        principal_id: str,
        tenant_id: str,
        project_id: str,
    ) -> None:
        if self._runtime_identity is not None:
            raise ValueError("Runtime identity is already bound.")
        if self.user_id is not None and self.user_id != principal_id:  # type: ignore[attr-defined]
            raise ValueError("Runtime principal does not match user_id.")
        self._runtime_identity = (principal_id, tenant_id, project_id)

    @property
    def runtime_identity(self) -> tuple[str, str, str] | None:
        return self._runtime_identity


class UserInput(_RuntimeIdentityBoundModel):
    """Basic user input for the agent."""

    message: str = Field(
        description="User input to the agent.",
        min_length=1,
        max_length=100_000,
        examples=["What is the weather in Tokyo?"],
    )
    model: SerializeAsAny[AllModelEnum] | None = Field(
        title="Model",
        description="LLM Model to use for the agent. Defaults to the default model set in the settings of the service.",
        default=None,
        examples=[OpenAIModelName.GPT_5_NANO, AnthropicModelName.HAIKU_45],
    )
    thread_id: str | None = Field(
        description="Thread ID to persist and continue a multi-turn conversation.",
        default=None,
        min_length=1,
        max_length=200,
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    user_id: str | None = Field(
        description="User ID to persist and continue a conversation across multiple threads.",
        default=None,
        min_length=1,
        max_length=200,
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    request_id: str | None = Field(
        description=(
            "Idempotency key. Reusing it with the same request returns or resumes "
            "the same run; reusing it with different input is rejected."
        ),
        default=None,
        min_length=8,
        max_length=200,
    )
    timeout_seconds: float | None = Field(
        description="Optional per-run timeout, capped by the service maximum.",
        default=None,
        ge=1,
        le=86_400,
    )
    agent_config: dict[str, Any] = Field(
        description="Additional configuration to pass through to the agent",
        default_factory=dict,
        examples=[{"spicy_level": 0.8}],
    )


class StreamInput(UserInput):
    """User input for streaming the agent's response."""

    stream_tokens: bool = Field(
        description="Whether to stream LLM tokens to the client.",
        default=True,
    )
    last_event_id: int = Field(
        description="Last received SSE event ID when reconnecting.",
        default=0,
        ge=0,
    )


class ToolCall(TypedDict):
    """Represents a request to call a tool."""

    name: str
    """The name of the tool to be called."""
    args: dict[str, Any]
    """The arguments to the tool call."""
    id: str | None
    """An identifier associated with the tool call."""
    type: NotRequired[Literal["tool_call"]]


class ChatMessage(BaseModel):
    """Message in a chat."""

    type: Literal["human", "ai", "tool", "custom"] = Field(
        description="Role of the message.",
        examples=["human", "ai", "tool", "custom"],
    )
    content: str = Field(
        description="Content of the message.",
        examples=["Hello, world!"],
    )
    tool_calls: list[ToolCall] = Field(
        description="Tool calls in the message.",
        default_factory=list,
    )
    tool_call_id: str | None = Field(
        description="Tool call that this message is responding to.",
        default=None,
        examples=["call_Jja7J89XsjrOLA5r!MEOW!SL"],
    )
    run_id: str | None = Field(
        description="Run ID of the message.",
        default=None,
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    response_metadata: dict[str, Any] = Field(
        description="Response metadata. For example: response headers, logprobs, token counts.",
        default_factory=dict,
    )
    custom_data: dict[str, Any] = Field(
        description="Custom message data.",
        default_factory=dict,
    )

    def pretty_repr(self) -> str:
        """Get a pretty representation of the message."""
        base_title = self.type.title() + " Message"
        padded = " " + base_title + " "
        sep_len = (80 - len(padded)) // 2
        sep = "=" * sep_len
        second_sep = sep + "=" if len(padded) % 2 else sep
        title = f"{sep}{padded}{second_sep}"
        return f"{title}\n\n{self.content}"

    def pretty_print(self) -> None:
        print(self.pretty_repr())  # noqa: T201


class Feedback(BaseModel):  # type: ignore[no-redef]
    """Feedback for a run, to record to LangSmith."""

    run_id: str = Field(
        description="Run ID to record feedback for.",
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    key: str = Field(
        description="Feedback key.",
        examples=["human-feedback-stars"],
    )
    score: float = Field(
        description="Feedback score.",
        examples=[0.8],
    )
    kwargs: dict[str, Any] = Field(
        description="Additional feedback kwargs, passed to LangSmith.",
        default={},
        examples=[{"comment": "In-line human feedback"}],
    )


class FeedbackResponse(BaseModel):
    status: Literal["success"] = "success"


class ChatHistoryInput(_RuntimeIdentityBoundModel):
    """Input for retrieving chat history."""

    thread_id: str = Field(
        description="Thread ID to persist and continue a multi-turn conversation.",
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )
    user_id: str | None = Field(
        description="User ID used to validate thread ownership. Must match the user_id that created the thread.",
        default=None,
        examples=["847c6285-8fc9-4560-a83f-4e6285809254"],
    )


class ChatHistory(BaseModel):
    messages: list[ChatMessage]


RunState = Literal[
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "timed_out",
]


class RunStatus(BaseModel):
    """Reader-facing lifecycle state for an invoke or streaming run."""

    request_id: str
    run_id: str | None = None
    kind: Literal["invoke", "stream"]
    status: RunState
    agent_id: str
    thread_id: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    event_count: int = 0
    oldest_event_id: int | None = None
    newest_event_id: int | None = None
    error_code: str | None = None
    error: str | None = None
    artifact_manifest: dict[str, Any] | None = None
    delivery_status: Literal[
        "execution_blocked",
        "delivered_with_issues",
        "release_ready",
    ] | None = None


class RunCancelResponse(BaseModel):
    request_id: str
    status: RunState
