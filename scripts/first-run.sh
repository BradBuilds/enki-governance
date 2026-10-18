#!/usr/bin/env bash
# first-run.sh — pull the local model. (Migrations run automatically when the
# API container starts; governance packs load on API boot.) Safe to re-run.
set -euo pipefail
[ -f .env ] && set -a && . ./.env && set +a
MODEL="${OLLAMA_MODEL:-gemma4:e2b}"

echo "waiting for Ollama…"
until docker compose exec -T ollama ollama list >/dev/null 2>&1; do sleep 2; done

echo "pulling local model '$MODEL' (first pull is slow, and slower on CPU)…"
docker compose exec -T ollama ollama pull "$MODEL"

echo "configuring the default AI provider → local Ollama…"
docker compose exec -T api python /app/scripts/configure-ai.py

echo
echo "✓ ready — open the UI (http://localhost:5173) and drop in a document,"
echo "  or run the worked example:  ./examples/run.sh"
echo "  (schema migrations already ran when the API container started.)"
