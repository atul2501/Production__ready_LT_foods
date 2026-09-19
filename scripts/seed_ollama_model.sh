#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-qwen2.5:7b-instruct}"

echo "Pulling Ollama model into the running 'ollama' container: $MODEL"
docker compose exec ollama ollama pull "$MODEL"
echo "Done. Set OLLAMA_MODEL=$MODEL in .env if it differs from the current value."
