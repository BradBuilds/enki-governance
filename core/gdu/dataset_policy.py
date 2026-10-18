"""
Dataset policy lookup — consulted by the entity resolver before any auto-merge
or new-entity creation. Each row in `dataset_policies` declares:

  trust_class      ∈ {canonical, corroborating, mentioned, review_required}
  merge_threshold  ∈ {identifier_only, name_plus_attr, name_plus_context, review_required}
  promotion_threshold ∈ {auto, two_independent_sources, manual_only}
  sensitivity      JSON {view_role_required, audit_on_view, redaction_aware}

When a source pipeline admits an entity, the resolver maps the pipeline name
to a dataset_id (via `pipeline_to_dataset_id`) and looks up its policy. If
the policy says `merge_threshold = review_required`, the resolver creates an
LDU entity AND a `collapse_candidate` row instead of auto-merging — that's
the mechanism that keeps manifest mentions from silently joining
a same-named canonical entity.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.gdu.dataset_policy")

# Cache so we don't hit PG on every admission. Invalidated when policies are
# updated via the API.
_CACHE: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 60.0  # seconds


# ─── Pipeline → dataset_id mapping ─────────────────────────────────────
# Source handlers use a pipeline string (e.g. "wikidata", "ofac", "Acme
# corpus ingester"). This maps each one to the canonical dataset_id used in
# dataset_policies. New datasets register themselves in `dataset_policies`
# and the resolver picks up their trust class automatically.

_PIPELINE_MAP = {
    # Wikipedia/Wikidata derived
    "wikidata": "wikidata",
    "wikipedia": "wikidata",
    # Refdata pack sources
    "ofac": "ofac_sdn",
    "ofac_sdn": "ofac_sdn",
    "un_sanctions": "un_consolidated",
    "opensanctions": "opensanctions_default",
    # Live API sources
    "sec_edgar": "sec_edgar",
    "fec": "_default",       # FEC candidates are corroborating, donors mentioned
    "courtlistener": "_default",
    "opencorporates": "_default",
    "gleif": "gleif",
    # Document ingestion sources — set explicitly per corpus
    "example_corpus_2024": "example_corpus_2024",
    "doj_phase1_corpus": "doj_phase1_corpus",
    "blue_book_nara": "blue_book_nara",
    "example_program": "example_program",
    "blackvault_foia": "blackvault_foia",
    "aaro_releases": "aaro_releases",
}


def pipeline_to_dataset_id(source_pipeline: str) -> str:
    """Map a source-pipeline string to its dataset_id for policy lookup.
    Unknown pipelines fall back to '_default'."""
    return _PIPELINE_MAP.get(source_pipeline, "_default")


def register_pipeline_mapping(pipeline: str, dataset_id: str) -> None:
    """Runtime registration — used when a new dataset is added via the API
    without code changes."""
    _PIPELINE_MAP[pipeline] = dataset_id


# ─── Policy lookup ──────────────────────────────────────────────────────

def get_policy(dataset_id: str) -> dict:
    """Return the policy row for `dataset_id`, falling back to `_default`.
    Cached for `_CACHE_TTL` seconds."""
    now = time.monotonic()
    cached = _CACHE.get(dataset_id)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    policy = _fetch_from_db(dataset_id)
    if not policy:
        # Fall through to default
        default = _CACHE.get("_default")
        if default and (now - default[1]) < _CACHE_TTL:
            return default[0]
        policy = _fetch_from_db("_default") or _hardcoded_default()

    _CACHE[dataset_id] = (policy, now)
    return policy


def _fetch_from_db(dataset_id: str) -> Optional[dict]:
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT dataset_id, name, trust_class, merge_threshold,
                           required_evidence, promotion_threshold,
                           sensitivity, extraction
                    FROM dataset_policies WHERE dataset_id = %s
                    """,
                    (dataset_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return {
                    "dataset_id": row[0],
                    "name": row[1],
                    "trust_class": row[2],
                    "merge_threshold": row[3],
                    "required_evidence": row[4] or [],
                    "promotion_threshold": row[5],
                    "sensitivity": row[6] or {},
                    "extraction": row[7] or {},
                }
    except Exception as e:
        log.debug("dataset_policies lookup failed for %s: %s", dataset_id, e)
        return None


def _hardcoded_default() -> dict:
    """Fallback when the DB is unreachable or the table doesn't exist yet.
    Matches the row INSERTed by migration 053."""
    return {
        "dataset_id": "_default",
        "name": "System default",
        "trust_class": "mentioned",
        "merge_threshold": "name_plus_context",
        "required_evidence": [],
        "promotion_threshold": "auto",
        "sensitivity": {},
        "extraction": {},
    }


def invalidate_cache(dataset_id: Optional[str] = None) -> None:
    """Drop cached policies. Called by the API when an operator updates one."""
    if dataset_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(dataset_id, None)


# ─── Predicates the resolver uses ──────────────────────────────────────

def should_block_auto_merge(source_pipeline: str) -> bool:
    """True iff the source's dataset_policy says we must NOT auto-merge into
    an existing canonical entity. The resolver, when this returns True,
    creates a separate LDU entity AND a `collapse_candidate` row instead.

    This is the single hot predicate that gates sensitive ingestion."""
    dataset_id = pipeline_to_dataset_id(source_pipeline)
    policy = get_policy(dataset_id)
    return policy.get("merge_threshold") == "review_required" or \
           policy.get("trust_class") == "review_required"


def required_evidence_for_merge(source_pipeline: str) -> list[str]:
    """List of evidence types required before auto-merge is allowed for this
    pipeline's dataset. Empty list means no extra requirement (normal merge
    rules apply)."""
    dataset_id = pipeline_to_dataset_id(source_pipeline)
    return get_policy(dataset_id).get("required_evidence", [])


def trust_class_for(source_pipeline: str) -> str:
    """Convenience accessor — returns the trust_class string."""
    dataset_id = pipeline_to_dataset_id(source_pipeline)
    return get_policy(dataset_id).get("trust_class", "mentioned")


def is_sensitive(source_pipeline: str) -> bool:
    """True if the pipeline's dataset is flagged sensitive (redaction-aware
    extraction, audit-on-view, role-gated)."""
    dataset_id = pipeline_to_dataset_id(source_pipeline)
    sens = get_policy(dataset_id).get("sensitivity") or {}
    return bool(sens.get("audit_on_view") or sens.get("view_role_required"))
