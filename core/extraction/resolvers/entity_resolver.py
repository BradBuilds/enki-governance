"""
Entity resolver — dedupes envelope.entities[] against the entities table.

Resolution order per entity:
    1. Pre-filter via is_resolvable_entity (skip URLs, hashes, etc.)
    2. If the entity has a canonical identifier type (email, mmsi, etc.):
       compute identifier_hash → SELECT by (tenant_id, identifier_hash).
       Hit → UPDATE last_seen + signal_count. Miss → INSERT with lineage.
    3. Without an identifier: SELECT by (tenant_id, normalized_name,
       canonical_type='person' by default). Hit → UPDATE. Miss → INSERT.
    4. Stamp envelope entity: node_id = "entity:<entity_id>",
       name_canonical = canonical_name from DB.

Errors per entity are logged and skipped — one bad row never aborts
the whole resolution pass.

Returns: dict of statistics {resolved: int, created: int, skipped: int,
failed: int} so the runner can report meaningful outcomes.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.extraction.resolvers.base import (
    canonical_type_for_identifier,
    compute_identifier_hash,
    is_resolvable_entity,
    normalize_name,
)
log = logging.getLogger("enki.extraction.resolvers.entity")


def _upsert_entity(
    conn,
    *,
    tenant_id: str,
    canonical_type: str,
    canonical_name: str,
    normalized: str,
    identifiers: dict,
    identifier_hash: Optional[str],
    ldu_t_id: Optional[int],
) -> Optional[int]:
    """Insert-or-update one entity through the GDU gateway.

    Phase 1.3: this function used to do its own INSERT INTO entities at
    two call sites (identifier_hash path + name path). Both are now
    routed through ``gateway.admit_entity`` so there's exactly one write
    authority for entity rows. The gateway handles dedup, base-60
    address assignment, lineage stamping, and admission logging.

    The trigram-similarity alias-merge path stays here as a *read-side*
    enhancement: we look first, and if a 0.55+ similarity match exists
    we update its alias array directly (no row created), then return.
    Only true new entities go through the gateway INSERT.
    """
    try:
        with conn.cursor() as cur:
            # Identifier-based exact dedup (skips trigram path)
            if identifier_hash:
                cur.execute(
                    """
                    SELECT entity_id FROM entities
                     WHERE tenant_id = %s AND identifier_hash = %s
                     LIMIT 1
                    """,
                    (tenant_id, identifier_hash),
                )
                row = cur.fetchone()
                if row:
                    entity_id = row[0]
                    cur.execute(
                        """
                        UPDATE entities
                           SET last_seen    = NOW(),
                               signal_count = signal_count + 1,
                               updated_at   = NOW()
                         WHERE entity_id = %s
                        """,
                        (entity_id,),
                    )
                    conn.commit()
                    return entity_id
                # Fall through to gateway INSERT — no existing match.
            else:
                # Trigram alias-merge: catches "V. Petrov" → existing
                # "Ivan Petrov" without creating a duplicate row.
                # Only runs when there's no identifier_hash to lock on.
                try:
                    cur.execute(
                        """
                        SELECT entity_id, similarity(normalized_name, %s) AS sim
                          FROM entities
                         WHERE tenant_id = %s
                           AND canonical_type = %s
                           AND normalized_name %% %s
                         ORDER BY sim DESC
                         LIMIT 1
                        """,
                        (normalized, tenant_id, canonical_type, normalized),
                    )
                    row = cur.fetchone()
                    if row and float(row[1] or 0) >= 0.55:
                        entity_id = row[0]
                        cur.execute(
                            """
                            UPDATE entities
                               SET aliases = (
                                       CASE
                                         WHEN %s = ANY(aliases) OR %s = normalized_name THEN aliases
                                         ELSE array_append(aliases, %s)
                                       END
                                   ),
                                   last_seen    = NOW(),
                                   signal_count = signal_count + 1,
                                   updated_at   = NOW()
                             WHERE entity_id = %s
                            """,
                            (normalized, normalized, normalized, entity_id),
                        )
                        conn.commit()
                        return entity_id
                except Exception as _trgm_e:
                    log.debug("trigram lookup failed: %s", _trgm_e)

            # No existing match — park in LDU. Phase 6 of the May 20
            # rebuild: capture-time admission moves to ldu_candidates;
            # the gateway only fires at promotion time once the
            # candidate has type evidence + sufficient signal.
            from core.ldu.admit_candidate import admit_candidate

            type_evidence = {
                k: v for k, v in (identifiers or {}).items()
                if k in ("wikidata_id", "wikidata_qid", "ein", "cik",
                         "duns", "lei", "mmsi", "imo", "icao24",
                         "registration", "tail_number", "ticker")
                and v
            } or None
            result = admit_candidate(
                surface_form=canonical_name,
                proposed_type=canonical_type,
                signal_rid=ldu_t_id or 0,
                tenant_id=tenant_id,
                type_evidence=type_evidence,
            )
            return result.entity_id  # set on attach, None otherwise
    except Exception as e:
        log.warning("admit_candidate(%s) failed: %s", canonical_name, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def resolve_entities(
    envelope: dict,
    conn,
    *,
    tenant_id: str = "default",
    ldu_t_id: Optional[int] = None,
) -> dict[str, int]:
    """Resolve every envelope.entities[] item and stamp node_ids.

    Mutates envelope in place. Returns counts dict.
    """
    stats = {"resolved": 0, "created": 0, "skipped": 0, "failed": 0}
    if conn is None:
        return stats

    stats.setdefault("ldup_routed", 0)
    stats.setdefault("cross_remapped", 0)

    entities = envelope.get("entities") or []
    for entity in entities:
        try:
            if not is_resolvable_entity(entity):
                stats["skipped"] += 1
                continue

            name_raw = (entity.get("name_raw") or "").strip()
            if not name_raw:
                stats["skipped"] += 1
                continue

            # ── LDU-W → GDU boundary check ────────────────────────────────
            # The pack-aware extractor stamps these flags. If they're
            # absent (legacy callers), validate now.
            if "_ldup_route" not in entity:
                from core.gdu.pack_aware_prompt import validate_llm_output
                ident_type = entity.get("identifier_type") or ""
                # legacy callers had identifier_type="named_<type>" — strip prefix
                raw_sub = ident_type[6:] if ident_type.startswith("named_") else ident_type
                v = validate_llm_output(
                    name=name_raw,
                    category=entity.get("_category") or "entity",
                    raw_subtype=raw_sub,
                    role=entity.get("role") or "mention",
                )
                entity["_ldup_route"] = not v.accepted
                entity["_category"] = v.category
                entity["_subtype"] = v.subtype
            else:
                # Trust extractor's validator. Pull the canonical subtype.
                # _subtype was set if accepted; otherwise None and routes to LDU-P.
                ident_type = entity.get("identifier_type") or ""
                if ident_type.startswith("named_") and ident_type != "named_unmappable":
                    entity.setdefault("_subtype", ident_type[6:])

            # Route unmappable items to LDU-P (provisional layer).
            if entity.get("_ldup_route"):
                _route_to_ldup(
                    conn,
                    tenant_id=tenant_id,
                    name_raw=name_raw,
                    raw_category=entity.get("_category") or "entity",
                    raw_subtype=entity.get("_subtype_raw") or "",
                    role=entity.get("role") or "mention",
                    reason=entity.get("_validator_reason") or "unknown",
                    ldu_t_id=ldu_t_id,
                )
                stats["ldup_routed"] += 1
                # Don't set node_id — caller knows this didn't land canonically
                continue

            # Cross-category remap?
            target_category = entity.get("_category") or "entity"
            if target_category != "entity":
                # Route to LDU (May 20 rebuild Phase 6). Place subtypes
                # (country, city, maritime_area, etc.) still go through
                # the gateway when admit_candidate.attach hits an existing
                # canonical row; otherwise they sit pending until the
                # gazetteer / research worker stamps type_evidence.
                from core.ldu.admit_candidate import admit_candidate
                canonical_subtype = entity.get("_subtype") or "country"
                result = admit_candidate(
                    surface_form=name_raw,
                    proposed_type=canonical_subtype,
                    signal_rid=ldu_t_id or 0,
                    tenant_id=tenant_id,
                )
                if result.entity_id:
                    entity["node_id"] = f"entity:{result.entity_id}"
                    entity["_target_category"] = target_category
                    entity["_target_subtype"] = canonical_subtype
                    stats["cross_remapped"] += 1
                else:
                    # Candidate parked in LDU — mentions wait for promotion.
                    stats["ldup_routed"] += 1
                continue

            # Standard entity write — only valid (entity, subtype) reach here.
            canonical_type = entity.get("_subtype") or "person"

            ident_legacy = entity.get("identifier_type")
            normalized = normalize_name(name_raw)
            identifiers: dict = {}
            if ident_legacy and not ident_legacy.startswith("named_"):
                identifiers[ident_legacy] = name_raw
            ihash = compute_identifier_hash(identifiers)

            entity_id = _upsert_entity(
                conn,
                tenant_id=tenant_id,
                canonical_type=canonical_type,
                canonical_name=name_raw,
                normalized=normalized,
                identifiers=identifiers,
                identifier_hash=ihash,
                ldu_t_id=ldu_t_id,
            )

            if entity_id is None:
                stats["failed"] += 1
                continue

            entity["node_id"] = f"entity:{entity_id}"
            entity["name_canonical"] = name_raw
            stats["resolved"] += 1
        except Exception as e:
            log.warning("resolve entity failed: %s", e)
            stats["failed"] += 1

    return stats


def _route_to_ldup(
    conn,
    *,
    tenant_id: str,
    name_raw: str,
    raw_category: str,
    raw_subtype: str,
    role: str,
    reason: str,
    ldu_t_id: Optional[int] = None,
) -> None:
    """Insert an unmappable LLM-extracted entity into entity_ldu_p.

    Schema (from migration 028): entity_ldu_p stores raw_name,
    suspected_type, rejection_reason, raw_payload, status. We pack our
    extra fields (raw_category, role) into raw_payload as JSON so the
    promoter has full context when reviewing.
    """
    import uuid
    ldu_p_id = f"ldup_{uuid.uuid4().hex[:12]}"
    raw_payload = {
        "raw_category": raw_category,
        "raw_subtype": raw_subtype,
        "role": role,
        "ldu_t_id": ldu_t_id,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO entity_ldu_p
                    (ldu_p_id, tenant_id, raw_name, suspected_type,
                     source_pipeline, rejection_reason, raw_payload, status)
                VALUES (%s, %s, %s, %s, 'entity_extractor', %s, %s, 'pending')
                ON CONFLICT (ldu_p_id) DO NOTHING
                """,
                (ldu_p_id, tenant_id, name_raw, raw_subtype,
                 reason, json.dumps(raw_payload)),
            )
    except Exception as e:
        log.debug("LDU-P route skipped for %s.%s '%s': %s",
                  raw_category, raw_subtype, name_raw, e)
