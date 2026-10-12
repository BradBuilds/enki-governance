"""Hermetic tests for the governance core: no database, no network, no model.

They cover the two claims the README asks a reader to check for themselves:
pack hashes reproduce from the files, and collapse decisions are deterministic.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
