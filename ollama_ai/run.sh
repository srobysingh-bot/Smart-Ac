#!/usr/bin/env bash
set -e
export OLLAMA_NUM_THREADS=2

ollama serve &

echo "Waiting for Ollama..."
until curl -s "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; do
  sleep 2
done

# Pull / warm model in sync with Hawaai DEFAULT_OLLAMA_MODEL (strict JSON + schema)
OLLAMA_WARMUP_MODEL="gemma:2b"

echo "Pulling model: ${OLLAMA_WARMUP_MODEL}..."
ollama pull "${OLLAMA_WARMUP_MODEL}" || echo "Model pull failed"

echo "Warming up model (loads weights before first Hawaai request)..."
ollama run "${OLLAMA_WARMUP_MODEL}" "warmup" || echo "Warmup failed"

# Background ping helps keep the model hot between requests (non-blocking for container)
( ollama run "${OLLAMA_WARMUP_MODEL}" "keep alive" > /dev/null 2>&1 || true ) &

wait
