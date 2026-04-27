#!/usr/bin/env bash
set -e
export OLLAMA_NUM_THREADS=2

ollama serve &

echo "Waiting for Ollama..."
until curl -s "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; do
  sleep 2
done

# Keep in sync with Hawaai DEFAULT_OLLAMA_MODEL (tinyllama = fast on Pi)
OLLAMA_WARMUP_MODEL="tinyllama"

echo "Pulling model: ${OLLAMA_WARMUP_MODEL}..."
ollama pull "${OLLAMA_WARMUP_MODEL}" || echo "Model pull failed"

echo "Warming up model (loads weights before first Hawaai request)..."
ollama run "${OLLAMA_WARMUP_MODEL}" "warmup" || echo "Warmup failed"

wait
