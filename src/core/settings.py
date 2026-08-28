from enum import StrEnum
from json import loads
from typing import Annotated, Any

from dotenv import find_dotenv
from pydantic import (
    BeforeValidator,
    Field,
    HttpUrl,
    SecretStr,
    TypeAdapter,
    computed_field,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from schema.models import (
    AllModelEnum,
    AnthropicModelName,
    AWSModelName,
    AzureOpenAIModelName,
    DeepseekModelName,
    FakeModelName,
    GoogleModelName,
    GroqModelName,
    OllamaModelName,
    OpenAICompatibleName,
    OpenAIModelName,
    OpenRouterModelName,
    Provider,
    VertexAIModelName,
)


class RunRegistryBackend(StrEnum):
    MEMORY = "memory"
    REDIS = "redis"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"

    def to_logging_level(self) -> int:
        """Convert to Python logging level constant."""
        import logging

        mapping = {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
            LogLevel.CRITICAL: logging.CRITICAL,
        }
        return mapping[self]


def check_str_is_http(x: str) -> str:
    http_url_adapter = TypeAdapter(HttpUrl)
    return str(http_url_adapter.validate_python(x))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=find_dotenv(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
        validate_default=False,
    )
    MODE: str | None = None

    HOST: str = "0.0.0.0"
    PORT: int = 8080
    GRACEFUL_SHUTDOWN_TIMEOUT: int = 30
    LOG_LEVEL: LogLevel = LogLevel.WARNING
    MAX_CONCURRENT_RUNS: int = Field(default=4, ge=1, le=64)
    MAX_QUEUED_RUNS: int = Field(default=16, ge=0, le=1_000)
    MAX_REQUEST_BODY_BYTES: int = Field(default=2_000_000, ge=1_024, le=100_000_000)
    RUN_TIMEOUT_SECONDS: float = Field(default=36_000, ge=1, le=86_400)
    SSE_HEARTBEAT_SECONDS: float = Field(default=15, ge=1, le=60)
    SSE_EVENT_BUFFER_SIZE: int = Field(default=4_096, ge=32, le=100_000)
    SSE_MAX_EVENT_BYTES: int = Field(default=1_000_000, ge=1_024, le=10_000_000)
    RUN_RETENTION_SECONDS: float = Field(default=3_600, ge=60, le=604_800)
    REQUIRE_AUTH_IN_PRODUCTION: bool = True

    # Distributed HTTP run control and SSE replay. PostgreSQL remains the
    # LangGraph checkpoint/store; Redis owns only live run coordination.
    RUN_REGISTRY_BACKEND: RunRegistryBackend = RunRegistryBackend.MEMORY
    REDIS_URL: SecretStr | None = None
    REDIS_KEY_PREFIX: str = Field(default="ratsnest", min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    REDIS_RUN_LEASE_SECONDS: int = Field(default=30, ge=5, le=300)
    REDIS_STREAM_BLOCK_MS: int = Field(default=5_000, ge=100, le=60_000)
    # Alert threshold only. Unpublished audit entries are never lossy-trimmed.
    REDIS_AUDIT_OUTBOX_MAXLEN: int = Field(default=100_000, ge=1_000, le=10_000_000)

    # Kafka is the durable audit/event backbone, fed by the Redis transactional
    # outbox. It intentionally does not carry LLM tokens or executable closures.
    KAFKA_AUDIT_ENABLED: bool = False
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_AUDIT_TOPIC: str = "ratsnest.audit.v1"
    KAFKA_AUDIT_CONSUMER_GROUP: str = "ratsnest-audit-relay-v1"
    KAFKA_CLIENT_ID: str = "ratsnest-agent-service"
    KAFKA_SECURITY_PROTOCOL: str = "PLAINTEXT"
    KAFKA_SASL_MECHANISM: str | None = None
    KAFKA_SASL_USERNAME: str | None = None
    KAFKA_SASL_PASSWORD: SecretStr | None = None
    KAFKA_AUDIT_BATCH_SIZE: int = Field(default=100, ge=1, le=1_000)
    KAFKA_AUDIT_POLL_MS: int = Field(default=1_000, ge=100, le=5_000)

    # Durable RatsNestPro Hardware Engineer execution. The service owns
    # LangGraph; a separate low-concurrency worker owns long EDA processes.
    RATSNESTPRO_TEMPORAL_ENABLED: bool = False
    TEMPORAL_ADDRESS: str = "localhost:7233"
    TEMPORAL_NAMESPACE: str = "default"
    TEMPORAL_API_KEY: SecretStr | None = None
    TEMPORAL_TLS: bool | None = None
    RATSNESTPRO_TEMPORAL_TASK_QUEUE: str = "ratsnest-hardware"
    RATSNESTPRO_TEMPORAL_WORKER_CONCURRENCY: int = Field(default=1, ge=1, le=8)
    RATSNESTPRO_TEMPORAL_WORKFLOW_TIMEOUT_SECONDS: int = Field(default=36_000, ge=60, le=86_400)
    RATSNESTPRO_TEMPORAL_STEP_TIMEOUT_SECONDS: int = Field(default=6000, ge=10, le=7_200)
    RATSNESTPRO_TEMPORAL_ROUTING_TIMEOUT_SECONDS: int = Field(default=1_800, ge=10, le=14_400)
    RATSNESTPRO_TEMPORAL_HEARTBEAT_SECONDS: int = Field(default=15, ge=1, le=300)
    RATSNESTPRO_TEMPORAL_POLL_SECONDS: float = Field(default=1.0, ge=0.1, le=30)
    RATSNESTPRO_TEMPORAL_RETRY_ATTEMPTS: int = Field(default=3, ge=1, le=5)
    RATSNESTPRO_TEMPORAL_GRACEFUL_SHUTDOWN_SECONDS: int = Field(default=30, ge=1, le=300)
    # The single-agent graph is an internal evaluation control. It is never
    # exposed by the product service unless an evaluator enables it explicitly.
    RATSNESTPRO_SINGLE_AGENT_EVAL_ENABLED: bool = False
    # Provider-visible Hardware Engineer output uses Redis only as a bounded
    # live bridge. The local JSONL transcript remains the audit/fallback copy.
    RATSNESTPRO_LLM_STREAM_ENABLED: bool = False
    RATSNESTPRO_LLM_STREAM_MAXLEN: int = Field(default=2_048, ge=32, le=20_000)
    RATSNESTPRO_LLM_STREAM_TTL_SECONDS: int = Field(default=86_400, ge=300, le=604_800)
    RATSNESTPRO_LLM_STREAM_SOCKET_TIMEOUT_SECONDS: float = Field(
        default=0.25, ge=0.05, le=5
    )
    # Individual non-Temporal calls must not monopolize a LangGraph run. Durable
    # Hardware Engineer work remains governed by the Temporal workflow budgets.
    RATSNESTPRO_AGENT_CALL_TIMEOUT_SECONDS: float = Field(default=900, ge=5, le=900)
    RATSNESTPRO_TOOL_CALL_TIMEOUT_SECONDS: float = Field(default=60, ge=5, le=300)

    AUTH_SECRET: SecretStr | None = None
    RATSNEST_INTERNAL_SIGNING_SECRET: SecretStr | None = Field(default=None, min_length=32)
    RATSNEST_INTERNAL_JWT_ISSUER: str = Field(
        default="ratsnest-control-plane", min_length=1, max_length=200
    )
    RATSNEST_INTERNAL_JWT_AUDIENCE: str = Field(
        default="ratsnest-agent-runtime", min_length=1, max_length=200
    )
    RATSNEST_INTERNAL_JWT_CLOCK_SKEW_SECONDS: int = Field(default=15, ge=0, le=60)
    RATSNEST_INTERNAL_JWT_MAX_TTL_SECONDS: int = Field(default=120, ge=10, le=300)
    RATSNEST_INTERNAL_GRPC_ENABLED: bool = False
    RATSNEST_INTERNAL_GRPC_HOST: str = "0.0.0.0"
    RATSNEST_INTERNAL_GRPC_PORT: int = Field(default=9090, ge=1, le=65_535)
    RATSNEST_INTERNAL_GRPC_SHUTDOWN_SECONDS: int = Field(default=10, ge=0, le=60)

    OPENAI_API_KEY: SecretStr | None = None
    DEEPSEEK_API_KEY: SecretStr | None = None
    DEEPSEEK_BASE_URL: Annotated[str, BeforeValidator(check_str_is_http)] = (
        "https://api.deepseek.com"
    )
    ANTHROPIC_API_KEY: SecretStr | None = None
    GOOGLE_API_KEY: SecretStr | None = None
    GOOGLE_APPLICATION_CREDENTIALS: SecretStr | None = None
    GROQ_API_KEY: SecretStr | None = None
    USE_AWS_BEDROCK: bool = False
    OLLAMA_MODEL: str | None = None
    OLLAMA_BASE_URL: str | None = None
    USE_FAKE_MODEL: bool = False
    OPENROUTER_API_KEY: str | None = None

    # If DEFAULT_MODEL is None, it will be set in model_post_init
    DEFAULT_MODEL: AllModelEnum | None = None  # type: ignore[assignment]
    AVAILABLE_MODELS: set[AllModelEnum] = set()  # type: ignore[assignment]

    # Set openai compatible api, mainly used for proof of concept
    COMPATIBLE_MODEL: str | None = None
    COMPATIBLE_API_KEY: SecretStr | None = None
    COMPATIBLE_BASE_URL: str | None = None

    # Optional purpose-aware OpenAI-compatible inference endpoints. Keeping
    # these empty preserves the selected provider; vLLM deployments can route
    # inexpensive intake/summary calls separately from engineering reasoning.
    INFERENCE_SMALL_BASE_URL: str | None = None
    INFERENCE_SMALL_MODEL: str | None = None
    INFERENCE_SMALL_API_KEY: SecretStr | None = None
    INFERENCE_LARGE_BASE_URL: str | None = None
    INFERENCE_LARGE_MODEL: str | None = None
    INFERENCE_LARGE_API_KEY: SecretStr | None = None

    LANGCHAIN_TRACING_V2: bool = False
    LANGCHAIN_PROJECT: str = "default"
    LANGCHAIN_ENDPOINT: Annotated[str, BeforeValidator(check_str_is_http)] = (
        "https://api.smith.langchain.com"
    )
    LANGCHAIN_API_KEY: SecretStr | None = None

    LANGFUSE_TRACING: bool = False
    LANGFUSE_HOST: Annotated[str, BeforeValidator(check_str_is_http)] = "https://cloud.langfuse.com"
    LANGFUSE_PUBLIC_KEY: SecretStr | None = None
    LANGFUSE_SECRET_KEY: SecretStr | None = None

    # PostgreSQL Configuration
    POSTGRES_USER: str | None = None
    POSTGRES_PASSWORD: SecretStr | None = None
    POSTGRES_HOST: str | None = None
    POSTGRES_PORT: int | None = None
    POSTGRES_DB: str | None = None
    POSTGRES_APPLICATION_NAME: str = "kicad-design-multi-agent-system"
    POSTGRES_MIN_CONNECTIONS_PER_POOL: int = 1
    POSTGRES_MAX_CONNECTIONS_PER_POOL: int = 1

    # Tenant-scoped cross-conversation memory. Embeddings can come from any
    # OpenAI-compatible endpoint; the deterministic local fallback keeps the
    # product usable without another external service.
    LONG_TERM_MEMORY_ENABLED: bool = True
    LONG_TERM_MEMORY_EMBEDDING_DIMENSIONS: int = Field(default=384, ge=64, le=4096)
    LONG_TERM_MEMORY_EMBEDDING_BASE_URL: str | None = None
    LONG_TERM_MEMORY_EMBEDDING_MODEL: str | None = None
    LONG_TERM_MEMORY_EMBEDDING_API_KEY: SecretStr | None = None
    LONG_TERM_MEMORY_RETRIEVAL_LIMIT: int = Field(default=8, ge=1, le=20)
    LONG_TERM_MEMORY_RECENCY_HALF_LIFE_DAYS: float = Field(default=30, ge=1, le=3650)
    LONG_TERM_MEMORY_MIN_SCORE: float = Field(default=0.22, ge=0, le=1)
    LONG_TERM_MEMORY_RETENTION_DAYS: int = Field(default=365, ge=1, le=3650)

    # Azure OpenAI Settings
    AZURE_OPENAI_API_KEY: SecretStr | None = None
    AZURE_OPENAI_ENDPOINT: str | None = None
    AZURE_OPENAI_API_VERSION: str = "2024-02-15-preview"
    AZURE_OPENAI_DEPLOYMENT_MAP: dict[str, str] = Field(
        default_factory=dict, description="Map of model names to Azure deployment IDs"
    )

    def model_post_init(self, __context: Any) -> None:
        api_keys = {
            Provider.OPENAI: self.OPENAI_API_KEY,
            Provider.OPENAI_COMPATIBLE: self.COMPATIBLE_BASE_URL and self.COMPATIBLE_MODEL,
            Provider.DEEPSEEK: self.DEEPSEEK_API_KEY,
            Provider.ANTHROPIC: self.ANTHROPIC_API_KEY,
            Provider.GOOGLE: self.GOOGLE_API_KEY,
            Provider.VERTEXAI: self.GOOGLE_APPLICATION_CREDENTIALS,
            Provider.GROQ: self.GROQ_API_KEY,
            Provider.AWS: self.USE_AWS_BEDROCK,
            Provider.OLLAMA: self.OLLAMA_MODEL,
            Provider.FAKE: self.USE_FAKE_MODEL,
            Provider.AZURE_OPENAI: self.AZURE_OPENAI_API_KEY,
            Provider.OPENROUTER: self.OPENROUTER_API_KEY,
        }
        active_keys = [k for k, v in api_keys.items() if v]

        # USE_FAKE_MODEL must win the default even when real provider keys are present.
        if self.USE_FAKE_MODEL and self.DEFAULT_MODEL is None:
            self.DEFAULT_MODEL = FakeModelName.FAKE

        for provider in active_keys:
            match provider:
                case Provider.OPENAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenAIModelName.GPT_5_NANO
                    self.AVAILABLE_MODELS.update(set(OpenAIModelName))
                case Provider.OPENAI_COMPATIBLE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenAICompatibleName.OPENAI_COMPATIBLE
                    self.AVAILABLE_MODELS.update(set(OpenAICompatibleName))
                case Provider.DEEPSEEK:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = DeepseekModelName.DEEPSEEK_V4_FLASH
                    self.AVAILABLE_MODELS.update(set(DeepseekModelName))
                case Provider.ANTHROPIC:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AnthropicModelName.HAIKU_45
                    self.AVAILABLE_MODELS.update(set(AnthropicModelName))
                case Provider.GOOGLE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = GoogleModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(GoogleModelName))
                case Provider.VERTEXAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = VertexAIModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(VertexAIModelName))
                case Provider.GROQ:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = GroqModelName.LLAMA_31_8B
                    self.AVAILABLE_MODELS.update(set(GroqModelName))
                case Provider.AWS:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AWSModelName.BEDROCK_HAIKU
                    self.AVAILABLE_MODELS.update(set(AWSModelName))
                case Provider.OLLAMA:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OllamaModelName.OLLAMA_GENERIC
                    self.AVAILABLE_MODELS.update(set(OllamaModelName))
                case Provider.OPENROUTER:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = OpenRouterModelName.GEMINI_36_FLASH
                    self.AVAILABLE_MODELS.update(set(OpenRouterModelName))
                case Provider.FAKE:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = FakeModelName.FAKE
                    self.AVAILABLE_MODELS.update(set(FakeModelName))
                case Provider.AZURE_OPENAI:
                    if self.DEFAULT_MODEL is None:
                        self.DEFAULT_MODEL = AzureOpenAIModelName.AZURE_GPT_5_MINI
                    self.AVAILABLE_MODELS.update(set(AzureOpenAIModelName))
                    # Validate Azure OpenAI settings if Azure provider is available
                    if not self.AZURE_OPENAI_API_KEY:
                        raise ValueError("AZURE_OPENAI_API_KEY must be set")
                    if not self.AZURE_OPENAI_ENDPOINT:
                        raise ValueError("AZURE_OPENAI_ENDPOINT must be set")
                    if not self.AZURE_OPENAI_DEPLOYMENT_MAP:
                        raise ValueError("AZURE_OPENAI_DEPLOYMENT_MAP must be set")

                    # Parse deployment map if it's a string
                    if isinstance(self.AZURE_OPENAI_DEPLOYMENT_MAP, str):
                        try:
                            self.AZURE_OPENAI_DEPLOYMENT_MAP = loads(
                                self.AZURE_OPENAI_DEPLOYMENT_MAP
                            )
                        except Exception as e:
                            raise ValueError(f"Invalid AZURE_OPENAI_DEPLOYMENT_MAP JSON: {e}")

                    # Validate required deployments exist
                    required_models = {"gpt-5", "gpt-5-mini"}
                    missing_models = required_models - set(self.AZURE_OPENAI_DEPLOYMENT_MAP.keys())
                    if missing_models:
                        raise ValueError(f"Missing required Azure deployments: {missing_models}")
                case _:
                    raise ValueError(f"Unknown provider: {provider}")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def BASE_URL(self) -> str:
        return f"http://{self.HOST}:{self.PORT}"

    def is_dev(self) -> bool:
        return self.MODE == "dev"

    def is_production(self) -> bool:
        return (self.MODE or "").lower() in {"prod", "production"}


settings = Settings()
