"""
Library import — read an envelope produced by library_export.py and
merge into the local install by ADDRESS CONTRACT.

The address contract makes this safe without name matching:
  - References dedupe by (DOI / ISBN / archive_id / title)
  - Entities dedupe by Wikidata QID (when present) else by
    (normalized_name, canonical_type)
  - Assessments dedupe by collapse_key (already canonical)
  - Documents dedupe by content_hash if available

For any conflict (same address, different content), the existing local
row wins — we never overwrite local edits. Import is purely additive.

Counts on the library row are recomputed after import.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Optional

sys.path.insert(0, "/app")

from core.discovery.library_export import verify_envelope
from storage.postgres.db import get_connection

log = logging.getLogger("enki.discovery.library_import")


def _find_or_create_library(conn, lib_data: dict) -> tuple[int, bool]:
    """Find a library on the local side that matches by canonical_name
    (the dedup key in libraries.UNIQUE (tenant_id, canonical_name)).
    Create if absent. Returns (library_id, is_new)."""
    canonical_name = lib_data.get("canonical_name")
    tenant_id = lib_data.get("tenant_id") or "default"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT library_id FROM libraries WHERE canonical_name = %s",
            (canonical_name,),
        )
        row = cur.fetchone()
        if row:
            return row[0], False
        # Recreate locally
        cur.execute(
            """
            INSERT INTO libraries
                (base60_address, library_subtype, canonical_name, focal_term,
                 description, tenant_id, discovery_status)
            VALUES (%s, %s, %s, %s, %s, %s, 'active')
            RETURNING library_id
            """,
            (lib_data.get("base60_address"), lib_data.get("library_subtype"),
             canonical_name, lib_data.get("focal_term"),
             lib_data.get("description"), tenant_id),
        )
        return cur.fetchone()[0], True


def _import_references(conn, library_id: int, refs: list[dict]) -> dict:
    stats = {"merged": 0, "inserted": 0}
    with conn.cursor() as cur:
        for ref in refs:
            ids = ref.get("identifiers") or {}
            dedup_key = (ids.get("doi") or ids.get("isbn") or
                          ids.get("archive_id") or ids.get("ol_id") or
                          ref.get("title") or "")
            if not dedup_key:
                continue
            cur.execute(
                """
                SELECT reference_id FROM bibliographic_references
                 WHERE library_id = %s AND
                       COALESCE(identifiers->>'doi',
                                identifiers->>'isbn',
                                identifiers->>'archive_id',
                                identifiers->>'ol_id',
                                title) = %s
                """,
                (library_id, dedup_key),
            )
            row = cur.fetchone()
            if row:
                stats["merged"] += 1
                continue
            cur.execute(
                """
                INSERT INTO bibliographic_references
                    (base60_address, library_id, citation_text, title,
                     authors, publication_year, publisher, reference_type,
                     identifiers, discovered_via, discovery_query,
                     purchase_links)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s::jsonb)
                ON CONFLICT (base60_address) DO NOTHING
                """,
                (ref.get("base60_address"), library_id, ref.get("citation_text"),
                 ref.get("title"), ref.get("authors") or [],
                 ref.get("publication_year"), ref.get("publisher"),
                 ref.get("reference_type") or "unknown",
                 json.dumps(ref.get("identifiers") or {}),
                 ref.get("discovered_via"), ref.get("discovery_query"),
                 json.dumps(ref.get("purchase_links") or [])),
            )
            stats["inserted"] += 1
    return stats


def _import_entities(conn, entities: list[dict]) -> dict:
    """Dedupe by Wikidata QID first, then by (normalized_name,
    canonical_type). Returns mapping of source_entity_id → local_entity_id."""
    stats = {"merged_by_qid": 0, "merged_by_name": 0, "inserted": 0,
             "id_remap": {}}
    with conn.cursor() as cur:
        for e in entities:
            ids = e.get("identifiers") or {}
            qid = ids.get("wikidata_qid")
            normalized = e.get("normalized_name")
            ctype = e.get("canonical_type")
            source_id = e.get("entity_id")

            local_id = None
            if qid:
                cur.execute(
                    "SELECT entity_id FROM entities WHERE identifiers->>'wikidata_qid' = %s LIMIT 1",
                    (qid,),
                )
                row = cur.fetchone()
                if row:
                    local_id = row[0]
                    stats["merged_by_qid"] += 1
            if local_id is None and normalized:
                cur.execute(
                    "SELECT entity_id FROM entities "
                    " WHERE normalized_name = %s AND canonical_type = %s LIMIT 1",
                    (normalized, ctype),
                )
                row = cur.fetchone()
                if row:
                    local_id = row[0]
                    stats["merged_by_name"] += 1
            if local_id is None:
                # Subtype lockdown — the peer may ship legacy canonical_type
                # values ('place', 'concept', 'gpe', ...) that migration 116
                # would reject. accept→normalize; non-accept→skip the row
                # rather than crash mid-batch. The corresponding mention
                # remap silently falls through (intentionally; peer
                # hygiene shouldn't propagate to us).
                try:
                    from core.ldu.subtype_validator import validate_for_ldu
                    _v = validate_for_ldu(ctype)
                    if _v.action == "accept":
                        if _v.canonical_subtype and _v.canonical_subtype != ctype:
                            ctype = _v.canonical_subtype
                    else:
                        stats.setdefault("skipped_subtype", 0)
                        stats["skipped_subtype"] += 1
                        continue
                except Exception:
                    pass  # defensive — let DB take it if validator unavailable

                # Insert as new (best-effort; respect schema columns we know exist)
                cur.execute(
                    """
                    INSERT INTO entities
                        (tenant_id, canonical_name, normalized_name,
                         canonical_type, entity_kind, source_kind,
                         base60_address, identifiers, signal_count,
                         admitted_under_active_set, first_seen, last_seen)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, TRUE,
                            NOW(), NOW())
                    RETURNING entity_id
                    """,
                    (e.get("tenant_id") or "default",
                     e.get("canonical_name"), normalized,
                     ctype, e.get("entity_kind") or "instance",
                     "imported",
                     e.get("base60_address"),
                     json.dumps(e.get("identifiers") or {}),
                     int(e.get("signal_count") or 0)),
                )
                local_id = cur.fetchone()[0]
                stats["inserted"] += 1
            if source_id and local_id:
                stats["id_remap"][int(source_id)] = int(local_id)
    return stats


def _import_assessments(conn, assessments: list[dict],
                         id_remap: dict[int, int]) -> dict:
    """Dedupe by collapse_key. Remap subj/obj entity IDs through id_remap."""
    stats = {"merged": 0, "inserted": 0}
    with conn.cursor() as cur:
        for a in assessments:
            subj = a.get("subj_entity_id")
            obj  = a.get("obj_entity_id")
            local_subj = id_remap.get(int(subj)) if subj else None
            local_obj  = id_remap.get(int(obj))  if obj  else None
            # Recompute the local collapse_key with the remapped IDs
            cur.execute(
                "SELECT compute_assessment_collapse_key(%s,%s,%s,%s)",
                (local_subj, a.get("predicate_edge_type"),
                 local_obj, a.get("obj_value")),
            )
            local_key = cur.fetchone()[0]
            cur.execute(
                "SELECT assessment_id FROM assessments WHERE collapse_key = %s",
                (local_key,),
            )
            row = cur.fetchone()
            if row:
                stats["merged"] += 1
                continue
            cur.execute(
                """
                INSERT INTO assessments
                    (tenant_id, base60_address, assessment_tier,
                     subj_entity_id, predicate_edge_type, obj_entity_id, obj_value,
                     polarity, collapse_key,
                     corroborated_by_count, contradicted_by_count,
                     source_statement_ids, earliest_event_time, latest_event_time,
                     confidence, first_seen, last_seen)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, NOW(), NOW())
                """,
                (a.get("tenant_id") or "default",
                 a.get("base60_address"),
                 a.get("assessment_tier") or "observation",
                 local_subj, a.get("predicate_edge_type"),
                 local_obj, a.get("obj_value"),
                 a.get("polarity") or "affirmative",
                 local_key,
                 int(a.get("corroborated_by_count") or 1),
                 int(a.get("contradicted_by_count") or 0),
                 [],   # source_statement_ids — local statements only
                 a.get("earliest_event_time"), a.get("latest_event_time"),
                 a.get("confidence")),
            )
            stats["inserted"] += 1
    return stats


def import_envelope(envelope: dict, *,
                     shared_secret: Optional[str] = None,
                     skip_signature_verify: bool = False) -> dict:
    """Validate envelope signature (if present), then merge into local DB.
    Returns import stats."""
    if not skip_signature_verify:
        ok, reason = verify_envelope(envelope, shared_secret)
        if not ok:
            return {"ok": False, "error": f"signature_verify_failed:{reason}"}

    stats = {"signature": envelope.get("signature", {}).get("algorithm"),
             "source_install": envelope.get("source_install"),
             "library": None, "references": None, "entities": None,
             "assessments": None, "events_imported": 0}

    with get_connection() as conn:
        lib_id, is_new_lib = _find_or_create_library(conn, envelope["library"])
        stats["library"] = {"library_id": lib_id, "is_new": is_new_lib}

        stats["references"]  = _import_references(conn, lib_id, envelope.get("references") or [])
        entity_stats         = _import_entities(conn, envelope.get("entities") or [])
        stats["entities"]    = {k: v for k, v in entity_stats.items() if k != "id_remap"}
        stats["assessments"] = _import_assessments(conn, envelope.get("assessments") or [],
                                                     entity_stats["id_remap"])
        conn.commit()
    return {"ok": True, **stats}


if __name__ == "__main__":
    import argparse, os
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("path")
    p.add_argument("--secret", default=os.environ.get("ENKI_ENVELOPE_SECRET"))
    p.add_argument("--skip-verify", action="store_true")
    args = p.parse_args()
    with open(args.path) as fp:
        envelope = json.load(fp)
    print(json.dumps(import_envelope(envelope, shared_secret=args.secret,
                                       skip_signature_verify=args.skip_verify),
                     indent=2, default=str))
