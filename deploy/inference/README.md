# Optional vLLM inference plane

This overlay is deliberately separate from the product Compose stack so a CPU-only clean clone still starts. It provides three OpenAI-compatible endpoints: a small model for routing/chat/summarization, a large model for engineering reasoning/review, and a 384-dimensional embedding model for long-term memory.

The server enables vLLM continuous batching, SHA-256 prefix caching and configurable quantized KV cache. Weight quantization is configurable as `awq`, `gptq`, or empty when the checkpoint already declares its format. The large endpoint can optionally use a draft model through `VLLM_SPECULATIVE_MODEL`; never enable speculative decoding without measuring output quality and accepted-token rate on the project eval suite.

Start only on an NVIDIA host with enough aggregate VRAM:

```powershell
$env:VLLM_SMALL_MODEL = "your-org/small-awq-model"
$env:VLLM_LARGE_MODEL = "your-org/large-awq-model"
docker compose -f deploy/inference/compose.vllm.yaml up -d
```

Connect the Runtime:

```dotenv
INFERENCE_SMALL_BASE_URL=http://host.docker.internal:8011/v1
INFERENCE_SMALL_MODEL=ratsnest-small
INFERENCE_LARGE_BASE_URL=http://host.docker.internal:8012/v1
INFERENCE_LARGE_MODEL=ratsnest-large
LONG_TERM_MEMORY_EMBEDDING_BASE_URL=http://host.docker.internal:8013/v1
LONG_TERM_MEMORY_EMBEDDING_MODEL=ratsnest-embedding
```

When these variables are absent, the user's selected provider remains authoritative and memory uses a deterministic local embedding fallback. This makes optimization reversible rather than a startup dependency.
