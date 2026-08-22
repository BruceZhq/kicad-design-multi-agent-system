#!/bin/sh
set -eu

set -- "$VLLM_MODEL" \
  --host 0.0.0.0 \
  --port "$VLLM_PORT" \
  --served-model-name "$VLLM_SERVED_MODEL" \
  --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION" \
  --max-model-len "$VLLM_MAX_MODEL_LEN" \
  --max-num-seqs "$VLLM_MAX_NUM_SEQS" \
  --enable-prefix-caching \
  --prefix-caching-hash-algo sha256

if [ -n "${VLLM_RUNNER:-}" ]; then
  set -- "$@" --runner "$VLLM_RUNNER"
fi
if [ -n "${VLLM_QUANTIZATION:-}" ]; then
  set -- "$@" --quantization "$VLLM_QUANTIZATION"
fi
if [ -n "${VLLM_KV_CACHE_DTYPE:-}" ]; then
  set -- "$@" --kv-cache-dtype "$VLLM_KV_CACHE_DTYPE"
fi
if [ -n "${VLLM_SPECULATIVE_MODEL:-}" ]; then
  speculative=$(printf '{"model":"%s","num_speculative_tokens":%s}' \
    "$VLLM_SPECULATIVE_MODEL" "${VLLM_SPECULATIVE_TOKENS:-5}")
  set -- "$@" --speculative-config "$speculative"
fi

exec vllm serve "$@"
