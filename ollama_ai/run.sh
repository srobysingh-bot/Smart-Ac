#!/usr/bin/env bash
set -e
export OLLAMA_NUM_THREADS=2

ollama serve &

echo "Waiting for Ollama..."
until curl -s "http://127.0.0.1:11434/api/tags" >/dev/null 2>&1; do
  sleep 2
done

echo "Pulling model..."
ollama pull gemma:2b-instruct || echo "Model pull failed"

wait
