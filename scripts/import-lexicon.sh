#!/usr/bin/env bash
# import-lexicon.sh — OPTIONAL. Import the WordNet sense-disambiguation layer.
#
# Core collapse works WITHOUT this. Run it to enable sense grounding (mapping words
# to concrete senses). It builds two packs from Open English WordNet (oewn:2023,
# CC-BY-4.0) via the `wn` library, then loads them into the governance spine.
# Heavy: downloads OEWN (~35 MB) and builds ~120k senses. Needs network.
set -euo pipefail
WN=/app/libraries/wordnet/definitions
SF=/app/libraries/sense_frequency/definitions

echo "[1/5] installing the 'wn' lexicon library in the api container…"
docker compose exec -T api pip install --quiet 'wn>=0.9'

echo "[2/5] downloading Open English WordNet (oewn:2023)…"
docker compose exec -T api python -m wn download oewn:2023

echo "[3/5] building the WordNet senses pack…"
docker compose exec -T api sh -c "mkdir -p $WN && python /app/scripts/build_wordnet_library.py oewn:2023 $WN/wordnet.senses.json"

echo "[4/5] building the sense-frequency pack…"
docker compose exec -T api sh -c "mkdir -p $SF && python /app/scripts/build_sense_frequency.py oewn:2023 $SF/sense_frequency.json"

echo "[5/5] loading the sense layer into the governance spine…"
docker compose exec -T api python /app/scripts/load_wordnet.py
docker compose exec -T api python /app/scripts/load_sense_frequency.py "$SF/sense_frequency.json"

echo
echo "✓ WordNet sense layer imported (Open English WordNet oewn:2023, CC-BY-4.0)."
