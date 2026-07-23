#!/usr/bin/env bash
# run.sh — collapse examples/sample-memo.txt into a governed fact graph.
# Prereqs: the stack is up (docker compose up -d && ./scripts/first-run.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

echo "→ copying the sample into the api container…"
docker compose cp examples/sample-memo.txt api:/tmp/sample-memo.txt
docker compose cp examples/collapse_example.py api:/tmp/collapse_example.py

echo "→ ingest → OCR/text → extract → GOVERNED COLLAPSE (local LLM)…"
docker compose exec -T api python /tmp/collapse_example.py
