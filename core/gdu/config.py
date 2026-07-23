"""
GDU Configuration

Single source of truth for GDU-related paths and settings.

All paths are resolved to ABSOLUTE paths from the project root.
The project root is determined by walking up from this file
(core/gdu/config.py → core/gdu → core → PROJECT_ROOT).

This is critical because the API server runs with
WorkingDirectory=/opt/enki/services/api/src, so relative paths
like Path("active_set_v5.json") would resolve to the wrong location.
"""

from pathlib import Path

# Project root: this file is at core/gdu/config.py → 3 levels up
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# Active set registry path (ABSOLUTE, resolved from project root)
# v6 adds entityvocab — canonical entity resolution rules, enrichment
# types, governed LLM arbitration prompt. Unifies the three entity layers
# (signal ingest, knowledge nodes, watch topics) under one resolver.
# Activated 2026-04-11
ACTIVE_SET_PATH = PROJECT_ROOT / "active_set_v6.json"

# Known pack paths (ABSOLUTE, resolved from project root)
PACK_PATHS = {
    # v4 packs (preserved unchanged in v5)
    "identity":          PROJECT_ROOT / "libraries" / "identity",
    "assertionvocab":    PROJECT_ROOT / "libraries" / "assertionvocab",
    "auditvocab":        PROJECT_ROOT / "libraries" / "auditvocab",
    "base60":            PROJECT_ROOT / "libraries" / "base60",
    "language":          PROJECT_ROOT / "libraries" / "language",
    "numeric":           PROJECT_ROOT / "libraries" / "numeric",
    "signalvocab":       PROJECT_ROOT / "libraries" / "signalvocab",
    "alertvocab":        PROJECT_ROOT / "libraries" / "alertvocab",
    "casevocab":         PROJECT_ROOT / "libraries" / "casevocab",
    # v5 additions (knowledge node rebuild vocabulary)
    "envelopevocab":     PROJECT_ROOT / "libraries" / "envelopevocab",
    "nodevocab":         PROJECT_ROOT / "libraries" / "nodevocab",
    "edgevocab":         PROJECT_ROOT / "libraries" / "edgevocab",
    "completenessvocab": PROJECT_ROOT / "libraries" / "completenessvocab",
    "intentvocab":       PROJECT_ROOT / "libraries" / "intentvocab",
    "relevancevocab":    PROJECT_ROOT / "libraries" / "relevancevocab",
    # v6 addition (entity resolution governance)
    "entityvocab":       PROJECT_ROOT / "libraries" / "entityvocab",
}
