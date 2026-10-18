"""
Structural integrity audit for the cyclical model.

Walks every layer and reports every misshapen thing. Each check returns:
  { check: str, severity: 'error'|'warning'|'info', count: int,
    sample: [...up to 5 examples], description: str }

Pure read-only. Run before any UI work to make sure the data layer
is sound. Result feeds an /admin/integrity dashboard later.

Eight audit categories:
  1. address  — every node has a base-60 address; no duplicates
  2. concept  — concept↔instance type match; no floating instances
  3. edges    — entity_relationships respect edgevocab type pairs
  4. statements — collapse_key dedup, corroboration counts match arrays
  5. events   — participants valid, addresses present, sane time
  6. library  — counters match members, no orphans
  7. dictionary — forms ↔ mentions, lemmas ↔ forms, predicates ↔ edge_types
  8. discovery_signal — candidates surfaced, hot mismatches flagged

Run inside the api container:
    docker exec app-api-1 python -m core.audit.structure [--severity error]
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Optional

sys.path.insert(0, "/app")

from storage.postgres.db import get_connection

log = logging.getLogger("enki.audit.structure")


def _rows(cur, sql: str, params: tuple = ()) -> list[tuple]:
    cur.execute(sql, params)
    return cur.fetchall()


def _scalar(cur, sql: str, params: tuple = ()) -> int:
    cur.execute(sql, params)
    r = cur.fetchone()
    return int(r[0]) if r and r[0] is not None else 0


def _check(name: str, severity: str, count: int, description: str,
            sample: list[Any] = None) -> dict:
    return {
        "check": name,
        "severity": severity,
        "count": count,
        "description": description,
        "sample": sample or [],
    }


# ─── 1. ADDRESS-SPACE INTEGRITY ───────────────────────────────────────

def audit_addresses(cur) -> list[dict]:
    out: list[dict] = []

    # Instances without an address
    n = _scalar(cur,
        "SELECT COUNT(*) FROM entities WHERE entity_kind='instance' AND base60_address IS NULL")
    out.append(_check("instances_without_address",
                       "warning" if n > 0 else "info", n,
                       "Instances should have a base-60 address (some legacy rows may lack one)"))

    # Concepts without an address
    n = _scalar(cur,
        "SELECT COUNT(*) FROM entities WHERE entity_kind='concept' AND base60_address IS NULL")
    out.append(_check("concepts_without_address",
                       "error" if n > 0 else "info", n,
                       "Concepts MUST have addresses"))

    # Duplicate addresses in entities
    dups = _rows(cur,
        """SELECT base60_address, COUNT(*) FROM entities
            WHERE base60_address IS NOT NULL GROUP BY 1 HAVING COUNT(*)>1
            ORDER BY 2 DESC LIMIT 5""")
    out.append(_check("entity_address_duplicates",
                       "error" if dups else "info", len(dups),
                       "Two entities should never share a base-60 address",
                       [{"address": d[0], "count": d[1]} for d in dups]))

    # Addresses outside valid ranges
    invalid = _rows(cur,
        """SELECT entity_id, canonical_name, base60_address FROM entities
            WHERE base60_address IS NOT NULL
              AND base60_address NOT SIMILAR TO '[0-9]+\\.[0-9]+\\.[0-9]+'
            LIMIT 5""")
    out.append(_check("malformed_addresses", "error" if invalid else "info",
                       len(invalid), "Address must be N.N.N",
                       [{"entity_id": r[0], "name": r[1], "addr": r[2]} for r in invalid]))

    return out


# ─── 2. CONCEPT ↔ INSTANCE INTEGRITY ─────────────────────────────────

def audit_concepts(cur) -> list[dict]:
    out: list[dict] = []

    # Instances linked to a concept of DIFFERENT type
    mismatches = _rows(cur,
        """SELECT i.entity_id, i.canonical_name, i.canonical_type AS inst_type,
                  c.canonical_name AS concept_name, c.canonical_type AS concept_type
             FROM entities i
             JOIN entities c ON c.entity_id = i.concept_entity_id
            WHERE i.entity_kind='instance'
              AND i.canonical_type <> c.canonical_type
            LIMIT 10""")
    out.append(_check("type_mismatch_instance_concept",
                       "error" if mismatches else "info", len(mismatches),
                       "Instance type should equal its concept's type",
                       [{"entity_id": r[0], "name": r[1], "inst_type": r[2],
                         "concept": r[3], "concept_type": r[4]} for r in mismatches]))

    # Instances pointing at non-concept parent
    bad_parent = _scalar(cur,
        """SELECT COUNT(*) FROM entities i
             JOIN entities c ON c.entity_id = i.concept_entity_id
            WHERE i.entity_kind='instance' AND c.entity_kind <> 'concept'""")
    out.append(_check("instance_parent_not_a_concept",
                       "error" if bad_parent > 0 else "info", bad_parent,
                       "Parent of an instance must itself be a concept"))

    # Concepts pointing UP to another concept (concepts should be top-level)
    nested = _scalar(cur,
        "SELECT COUNT(*) FROM entities WHERE entity_kind='concept' AND concept_entity_id IS NOT NULL")
    out.append(_check("nested_concepts",
                       "warning" if nested > 0 else "info", nested,
                       "Concepts should not have concept_entity_id set"))

    # Unlinked instances by type (where a concept of that type exists)
    unlinked = _rows(cur,
        """SELECT i.canonical_type, COUNT(*) AS unlinked
             FROM entities i
            WHERE i.entity_kind='instance' AND i.concept_entity_id IS NULL
              AND EXISTS (SELECT 1 FROM entities c
                           WHERE c.entity_kind='concept'
                             AND c.canonical_type = i.canonical_type)
            GROUP BY i.canonical_type ORDER BY 2 DESC LIMIT 5""")
    out.append(_check("instances_unlinked_when_concept_exists",
                       "warning" if unlinked else "info",
                       sum(r[1] for r in unlinked),
                       "Instances exist whose canonical_type already has a concept node",
                       [{"type": r[0], "unlinked_count": r[1]} for r in unlinked]))

    return out


# ─── 3. EDGES ────────────────────────────────────────────────────────

def audit_edges(cur) -> list[dict]:
    out: list[dict] = []

    # Invalid type pairs (using the validator we already wrote)
    n_total = _scalar(cur, "SELECT COUNT(*) FROM entity_relationships")
    n_invalid = _scalar(cur, "SELECT COUNT(*) FROM invalid_entity_relationships")
    out.append(_check("invalid_entity_relationships_type_pair",
                       "error" if n_invalid > 0 else "info", n_invalid,
                       f"Edges that violate edgevocab type-pair contract (of {n_total} total)"))

    # Symmetric edges with both directions stored
    asym = _rows(cur,
        """SELECT a.subj_entity_id, a.obj_entity_id, a.predicate_edge_type, c.is_symmetric
             FROM assessments a
             JOIN assessments b ON b.subj_entity_id = a.obj_entity_id
                              AND b.obj_entity_id = a.subj_entity_id
                              AND b.predicate_edge_type = a.predicate_edge_type
             JOIN gdu_type_constraints c ON c.edge_type = a.predicate_edge_type
            WHERE c.is_symmetric AND a.assessment_id < b.assessment_id
            LIMIT 5""")
    out.append(_check("symmetric_edge_both_directions",
                       "warning" if asym else "info", len(asym),
                       "Symmetric edges should collapse — both A→B and B→A rows exist",
                       [{"subj": r[0], "obj": r[1], "pred": r[2]} for r in asym]))

    # Edge proposals waiting for review (potential vocab extensions)
    n_pending = _scalar(cur, "SELECT COUNT(*) FROM edge_proposals WHERE status='pending'")
    out.append(_check("edge_proposals_pending_review",
                       "info", n_pending,
                       "Edges the validator deferred for analyst review"))

    return out


# ─── 4. STATEMENTS / ASSESSMENTS ──────────────────────────────────────

def audit_statements(cur) -> list[dict]:
    out: list[dict] = []

    # Assessments whose source_statement_ids count != corroborated_by_count
    bad_corr = _rows(cur,
        """SELECT assessment_id, corroborated_by_count, ARRAY_LENGTH(source_statement_ids, 1)
             FROM assessments
            WHERE corroborated_by_count <> COALESCE(ARRAY_LENGTH(source_statement_ids, 1), 0)
                + COALESCE(contradicted_by_count, 0)
            LIMIT 5""")
    out.append(_check("assessment_corroboration_mismatch",
                       "error" if bad_corr else "info", len(bad_corr),
                       "corroborated_by_count + contradicted_by_count should equal source_statement_ids length",
                       [{"assessment_id": r[0], "corroboration": r[1], "actual_ids": r[2]} for r in bad_corr]))

    # Duplicate collapse keys (impossible if UNIQUE constraint works)
    dupkey = _scalar(cur,
        """SELECT COUNT(*) FROM (
             SELECT collapse_key FROM assessments GROUP BY 1 HAVING COUNT(*)>1
           ) sub""")
    out.append(_check("assessment_duplicate_collapse_keys",
                       "error" if dupkey > 0 else "info", dupkey,
                       "Two assessments with the same collapse_key — UNIQUE constraint should prevent this"))

    # Orphan statement source IDs (assessment references statement that doesn't exist)
    orphans = _scalar(cur,
        """SELECT COUNT(DISTINCT s) FROM (
             SELECT UNNEST(source_statement_ids) AS s FROM assessments
           ) inner_q
            WHERE s IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM statements WHERE statement_id = inner_q.s)""")
    out.append(_check("assessment_orphan_statement_refs",
                       "error" if orphans > 0 else "info", orphans,
                       "Assessment's source_statement_ids points at deleted statements"))

    # Statement validation health
    by_status = _rows(cur,
        "SELECT validation_status, COUNT(*) FROM statements GROUP BY 1")
    out.append(_check("statement_status_distribution", "info",
                       sum(r[1] for r in by_status),
                       "Statement status breakdown",
                       [{"status": r[0], "count": r[1]} for r in by_status]))

    # Statements rejected with reasons we should know about
    by_reason = _rows(cur,
        """SELECT SUBSTRING(validation_reason FROM 1 FOR 40), COUNT(*)
             FROM statements WHERE validation_status='rejected'
            GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
    out.append(_check("statement_rejection_reasons", "info",
                       sum(r[1] for r in by_reason),
                       "Top reasons statements get rejected",
                       [{"reason": r[0], "count": r[1]} for r in by_reason]))

    return out


# ─── 5. EVENTS ────────────────────────────────────────────────────────

def audit_events(cur) -> list[dict]:
    out: list[dict] = []

    # Events without addresses (legacy + spawned)
    n_no_addr = _scalar(cur, "SELECT COUNT(*) FROM events WHERE base60_address IS NULL")
    n_with    = _scalar(cur, "SELECT COUNT(*) FROM events WHERE base60_address IS NOT NULL")
    out.append(_check("events_without_address",
                       "info", n_no_addr,
                       f"Legacy events from old signal pipeline ({n_with} have canonical addresses)"))

    # Events with no participants
    n_no_actors = _scalar(cur,
        """SELECT COUNT(*) FROM events
            WHERE base60_address IS NOT NULL
              AND (actor_entity_ids IS NULL OR ARRAY_LENGTH(actor_entity_ids, 1) IS NULL)""")
    out.append(_check("canonical_events_no_participants",
                       "warning" if n_no_actors > 0 else "info", n_no_actors,
                       "Canonical events should have participants"))

    # Events with originating statement that doesn't exist
    orphans = _scalar(cur,
        """SELECT COUNT(DISTINCT s) FROM (
             SELECT UNNEST(originating_statement_ids) AS s FROM events
              WHERE originating_statement_ids IS NOT NULL
           ) q
            WHERE s IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM statements WHERE statement_id = q.s)""")
    out.append(_check("events_orphan_statement_refs",
                       "error" if orphans > 0 else "info", orphans,
                       "Event references statements that no longer exist"))

    return out


# ─── 6. LIBRARIES ─────────────────────────────────────────────────────

def audit_libraries(cur) -> list[dict]:
    out: list[dict] = []

    # Libraries with counter drift (library.reference_count vs actual count)
    drifts = _rows(cur,
        """SELECT l.library_id, l.canonical_name, l.reference_count,
                  (SELECT COUNT(*) FROM bibliographic_references r WHERE r.library_id=l.library_id) AS actual
             FROM libraries l
            WHERE l.reference_count <> (SELECT COUNT(*) FROM bibliographic_references r WHERE r.library_id=l.library_id)
            LIMIT 5""")
    out.append(_check("library_reference_counter_drift",
                       "warning" if drifts else "info", len(drifts),
                       "library.reference_count should equal actual ref count",
                       [{"library_id": r[0], "name": r[1], "stored": r[2], "actual": r[3]} for r in drifts]))

    # Documents in library_members that don't exist
    orphan_docs = _scalar(cur,
        """SELECT COUNT(*) FROM library_members lm
            WHERE lm.member_kind='document'
              AND NOT EXISTS (SELECT 1 FROM documents WHERE document_id = lm.member_id)""")
    out.append(_check("library_members_orphan_docs",
                       "error" if orphan_docs > 0 else "info", orphan_docs,
                       "library_members.member_id points at deleted documents"))

    # References marked 'ingested' but no document_id linked
    bad_ingest = _scalar(cur,
        """SELECT COUNT(*) FROM bibliographic_references
            WHERE acquisition_status='ingested' AND document_id IS NULL""")
    out.append(_check("ingested_refs_missing_document",
                       "error" if bad_ingest > 0 else "info", bad_ingest,
                       "References marked 'ingested' should have a document_id"))

    return out


# ─── 7. DICTIONARY ────────────────────────────────────────────────────

def audit_dictionary(cur) -> list[dict]:
    out: list[dict] = []

    # Mentions whose surface_form_norm has no row in language_forms
    n = _scalar(cur,
        """SELECT COUNT(DISTINCT surface_form_norm) FROM document_mentions m
            WHERE surface_form_norm IS NOT NULL AND surface_form_norm <> ''
              AND NOT EXISTS (SELECT 1 FROM language_forms WHERE surface_norm = m.surface_form_norm)""")
    out.append(_check("mentions_without_form_row",
                       "warning" if n > 0 else "info", n,
                       "Every distinct mention surface should have a language_forms row"))

    # Forms not linked to a lemma
    n = _scalar(cur, "SELECT COUNT(*) FROM language_forms WHERE lemma_id IS NULL")
    out.append(_check("forms_without_lemma",
                       "warning" if n > 0 else "info", n,
                       "Every form should belong to a lemma"))

    # Predicate dict entries referencing edge_types that don't exist
    n = _scalar(cur,
        """SELECT COUNT(*) FROM predicate_dictionary
            WHERE NOT EXISTS (SELECT 1 FROM gdu_type_constraints WHERE edge_type = predicate_dictionary.edge_type)""")
    out.append(_check("predicate_dict_unknown_edge_type",
                       "error" if n > 0 else "info", n,
                       "predicate_dictionary should only reference valid edge types"))

    return out


# ─── 8. DISCOVERY SIGNAL ──────────────────────────────────────────────

def audit_discovery_signal(cur) -> list[dict]:
    out: list[dict] = []

    # Verb candidates with N+ occurrences not yet in dictionary
    candidates = _rows(cur,
        """SELECT predicate_lemma, COUNT(*) AS n FROM statements
            WHERE validation_status='rejected'
              AND validation_reason LIKE 'no_predicate_mapping:%%'
              AND predicate_lemma IS NOT NULL
            GROUP BY predicate_lemma HAVING COUNT(*) >= 3
            ORDER BY n DESC LIMIT 10""")
    out.append(_check("predicate_candidates_awaiting_promotion",
                       "warning" if candidates else "info", len(candidates),
                       "Verb lemmas the corpus keeps proposing but predicate_dictionary lacks",
                       [{"lemma": r[0], "occurrences": r[1]} for r in candidates]))

    # Edge_type_observations that look like vocab gaps
    edge_gaps = _scalar(cur,
        """SELECT COUNT(*) FROM edge_type_observations
            WHERE schema_status='candidate' AND proposal_count >= 5""")
    out.append(_check("edge_type_candidates_awaiting_promotion",
                       "warning" if edge_gaps > 0 else "info", edge_gaps,
                       "Edge type observations with ≥5 proposals that aren't in edgevocab"))

    return out


# ─── Orchestrator ────────────────────────────────────────────────────

ALL_AUDITS = [
    ("address",           audit_addresses),
    ("concept",           audit_concepts),
    ("edges",             audit_edges),
    ("statements",        audit_statements),
    ("events",            audit_events),
    ("libraries",         audit_libraries),
    ("dictionary",        audit_dictionary),
    ("discovery_signal",  audit_discovery_signal),
]


def run(severity_filter: Optional[str] = None) -> dict:
    """Run every audit. Returns nested results."""
    results: dict[str, list[dict]] = {}
    summary = {"error": 0, "warning": 0, "info": 0}

    with get_connection() as conn:
        with conn.cursor() as cur:
            for name, fn in ALL_AUDITS:
                try:
                    checks = fn(cur)
                except Exception as e:
                    log.exception("audit %s crashed", name)
                    checks = [_check(f"{name}_failed", "error", 1, str(e))]

                if severity_filter:
                    checks = [c for c in checks if c["severity"] == severity_filter]
                results[name] = checks
                for c in checks:
                    if c["count"] > 0 or c["severity"] != "info":
                        summary[c["severity"]] = summary.get(c["severity"], 0) + 1

    return {"summary": summary, "results": results}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--severity", default=None,
                   choices=["error", "warning", "info"])
    args = p.parse_args()
    print(json.dumps(run(args.severity), indent=2, default=str))
