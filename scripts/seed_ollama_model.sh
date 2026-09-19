#!/usr/bin/env bash
set -euo pipefail

# Only needed for the self-hosted Ollama fallback (see .env.example's commented block) -
# Ollama Cloud, the default, needs no local model pull.

MODEL="${1:-gpt-oss:20b}"

echo "Pulling Ollama model into the local native 'ollama serve' instance: $MODEL"
ollama pull "$MODEL"
echo "Done. Set OLLAMA_MODEL=$MODEL in .env if it differs from the current value."
