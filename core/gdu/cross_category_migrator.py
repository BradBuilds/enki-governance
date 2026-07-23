"""
Cross-category migrator — handles the "Egypt is a country, not an organization"
case correctly.

When a validation source (Wikidata, Wikipedia, etc.) produces evidence that
an entity belongs in a DIFFERENT GDU category than the one it was originally
written under, we must NOT overwrite canonical_type in place. That creates
incoherent rows (entities.canonical_type='country' is a category mismatch:
'country' is a place subtype, not an entity subtype).

The right move depends on how many candidate canonical entities the source
returned for the surface form:

  Tier 1 — UNCERTAIN (cross-category but no clear single Wikidata hit, or
           ambiguous evidence):
    • Mark the original row gdu_canonical=FALSE
    • Stamp metadata: {reclassification_pending: true, reason}
    • Do NOTHING else. The ldup_promoter worker handles cleanup.

  Tier 2 — SINGLE-HIT MIGRATION (cross-category AND one clear Wikidata
           candidate, e.g., a unique Q-ID):
    • Insert a NEW row in the correct master table (places, topics, …)
      with the canonical name + aliases including the original name.
    • Insert a redirect: original entity_id → new master_*_id (so old
      references resolve forward).
    • Mark the original entity row gdu_canonical=FALSE with metadata
      {reclassified_to: "place:42", reason}.
    • Touching the original row triggers the (new in mig 032) mirror
      function which updates the matching knowledge_node accordingly.

Tier 3 (multi-hit disambiguation — "Egypt the country vs. Egypt, Texas")
is OUT OF SCOPE here. That requires a disambiguation worker with article
context analysis. See docs/TIER_3_DISAMBIGUATION_PROMPT.md for the
research/build prompt.

This module never raises. Errors are logged and the caller continues.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("enki.gdu.cross_category_migrator")


@dataclass(frozen=True)
class MigrationResult:
    """Outcome of one cross-category attempt."""
    action: str           # "tier_1_pending" | "tier_2_migrated" | "noop" | "failed"
    new_node_id: Optional[str] = None
    reason: str = ""


# Post-unification (migration 118): places ARE entities. The migrator
# only needs to update canonical_type + ensure place_attributes sidecar.
# No separate "places" row insert; no entity_id → place_id redirect.
_MASTER_TABLE = {
    "entity":    "entities",
    "place":     "entities",
    "topic":     "topics",
    # Other categories don't have a simple 1:1 master table; they live as
    # rows directly in knowledge_nodes (events, assessments, etc.).
}


def _mark_for_reclassification(
    conn,
    *,
    tenant_id: str,
    entity_id: int,
    target_category: str,
    target_subtype: str,
    source: str,
    source_id: Optional[str],
    reason: str,
) -> None:
    """Tier 1 — drop the entity to LDU-P. Mark with reclassification metadata.

    The (now in mig 032) mirror trigger sees gdu_canonical=FALSE and
    correspondingly demotes the matching knowledge_node row to provisional.
    """
    metadata = {
        "reclassification_pending": True,
        "target_category": target_category,
        "target_subtype": target_subtype,
        "source": source,
        "source_id": source_id,
        "reason": reason,
    }
    with conn.cursor() as cur:
        # Update the entity row. canonical_type stays as-is (incoherent
        # but we're flagging it; the promoter will handle the move).
        cur.execute(
            """
            UPDATE entities
               SET gdu_canonical = FALSE,
                   envelope = COALESCE(envelope, '{}'::jsonb)
                              || jsonb_build_object('reclassification', %s::jsonb),
                   updated_at = NOW()
             WHERE entity_id = %s AND tenant_id = %s
            """,
            (json.dumps(metadata), entity_id, tenant_id),
        )


def _migrate_entity_to_place(
    conn,
    *,
    tenant_id: str,
    entity_id: int,
    canonical_name: str,
    aliases: list[str],
    target_subtype: str,
    source: str,
    source_id: Optional[str],
    profile: dict,
    completeness_score: float,
    completeness_tier: str,
) -> MigrationResult:
    """Tier 2 — in-place reclassification.

    Post-unification (migration 118), places ARE entities. This becomes
    a simple canonical_type update on the existing entity row, plus an
    insert into the place_attributes sidecar. No new entity_id is
    minted; existing references to entity:NNN continue to resolve.
    The entity now reports canonical_type='place'/'city'/'country'/etc.

    Returns the entity's node_id ("entity:NNN") so callers can update
    references — though no rewriting is actually needed since the ID
    didn't change.
    """
    new_aliases = list({*aliases, canonical_name})

    try:
        with conn.cursor() as cur:
            # 1. Update the entity's canonical_type + merge aliases.
            cur.execute(
                """
                UPDATE entities
                   SET canonical_type = %s,
                       aliases = (
                         SELECT array_agg(DISTINCT a)
                           FROM unnest(COALESCE(aliases, ARRAY[]::TEXT[]) || %s::TEXT[]) a
                       ),
                       envelope = COALESCE(envelope, '{}'::jsonb) || jsonb_build_object(
                         'reclassified_to_place', jsonb_build_object(
                            'source', %s,
                            'source_id', %s,
                            'completeness_score', %s,
                            'completeness_tier', %s,
                            'profile', %s::jsonb
                         )
                       ),
                       updated_at = NOW()
                 WHERE entity_id = %s AND tenant_id = %s
                """,
                (target_subtype, new_aliases,
                 source, source_id, completeness_score, completeness_tier,
                 json.dumps({"profile": profile}),
                 entity_id, tenant_id),
            )

            # 2. Ensure place_attributes sidecar exists. Extract any
            #    geo fields the profile carried; defaults to NULL coords
            #    until the resolver geocodes them.
            iso_country = profile.get("iso_country_code") if profile else None
            iso_region = profile.get("iso_region_code") if profile else None
            geonames_id = profile.get("geonames_id") if profile else None
            cur.execute(
                """
                INSERT INTO place_attributes
                  (entity_id, iso_country_code, iso_region_code, geonames_id,
                   geocode_source)
                VALUES (%s, %s, %s, %s, 'cross_category_migrator')
                ON CONFLICT (entity_id) DO UPDATE
                  SET iso_country_code = COALESCE(EXCLUDED.iso_country_code,
                                                  place_attributes.iso_country_code),
                      iso_region_code  = COALESCE(EXCLUDED.iso_region_code,
                                                  place_attributes.iso_region_code),
                      geonames_id      = COALESCE(EXCLUDED.geonames_id,
                                                  place_attributes.geonames_id),
                      updated_at = NOW()
                """,
                (entity_id, iso_country, iso_region, geonames_id),
            )

            # knowledge_edges already use entity:NNN node_id — no
            # rewriting needed since the entity_id didn't change.
            new_node_id = f"entity:{entity_id}"

        return MigrationResult(
            action="tier_2_migrated",
            new_node_id=new_node_id,
            reason="cross_category_single_hit",
        )
    except Exception as e:
        log.warning("entity→place migration failed (entity_id=%s, name=%r): %s",
                    entity_id, canonical_name, e)
        try:
            conn.rollback()
        except Exception:
            pass
        return MigrationResult(action="failed", reason=str(e))


# Public entry point ──────────────────────────────────────────────────────

def reclassify_entity(
    conn,
    *,
    tenant_id: str,
    entity_id: int,
    canonical_name: str,
    aliases: Optional[list[str]],
    target_category: str,
    target_subtype: str,
    source: str,
    source_id: Optional[str],
    profile: Optional[dict] = None,
    completeness_score: float = 0.6,
    completeness_tier: str = "partial",
    multi_candidate: bool = False,
) -> MigrationResult:
    """Single entry point for cross-category reclassification.

    Args:
        conn: psycopg2 connection (caller commits)
        tenant_id, entity_id: which entity to reclassify
        canonical_name: the row's canonical_name
        aliases: existing aliases (list of strings, may be None)
        target_category: where the validation evidence says it belongs
                         ("place", "topic", etc.)
        target_subtype: subtype within target category
        source: validation source name (e.g., "Wikidata")
        source_id: source's ID (e.g., "Q79")
        profile: enrichment profile from the source (population, etc.)
        completeness_score, completeness_tier: from the source
        multi_candidate: True if the source returned >1 candidate. Forces
                         Tier 1 (mark pending) instead of Tier 2 (migrate).

    Returns:
        MigrationResult describing what was done.
    """
    if target_category == "entity":
        # Same category, just a subtype refinement — caller can do that
        # directly. We don't migrate within entities here.
        return MigrationResult(action="noop", reason="same_category")

    aliases = list(aliases or [])
    profile = profile or {}

    # Tier 1 — uncertain or multi-candidate: just flag it
    if multi_candidate:
        try:
            _mark_for_reclassification(
                conn,
                tenant_id=tenant_id,
                entity_id=entity_id,
                target_category=target_category,
                target_subtype=target_subtype,
                source=source,
                source_id=source_id,
                reason="multi_candidate_disambiguation_pending",
            )
            log.info(
                "Entity %d (%s): cross-category multi-candidate → marked pending "
                "(%s.%s, source=%s)",
                entity_id, canonical_name, target_category, target_subtype, source,
            )
            return MigrationResult(action="tier_1_pending",
                                   reason="multi_candidate")
        except Exception as e:
            log.warning("mark_for_reclassification failed (entity %s): %s",
                        entity_id, e)
            return MigrationResult(action="failed", reason=str(e))

    # Tier 2 — single-hit cross-category migration
    if target_category == "place":
        return _migrate_entity_to_place(
            conn,
            tenant_id=tenant_id,
            entity_id=entity_id,
            canonical_name=canonical_name,
            aliases=aliases,
            target_subtype=target_subtype,
            source=source,
            source_id=source_id,
            profile=profile,
            completeness_score=completeness_score,
            completeness_tier=completeness_tier,
        )

    # Other target categories (topic, etc.) — fall through to Tier 1 for now.
    # Adding migrate_entity_to_topic etc. follows the same pattern as the
    # place migrator above; do it when we see it actually happen at scale.
    try:
        _mark_for_reclassification(
            conn,
            tenant_id=tenant_id,
            entity_id=entity_id,
            target_category=target_category,
            target_subtype=target_subtype,
            source=source,
            source_id=source_id,
            reason=f"target_category_{target_category}_migrator_not_implemented",
        )
        return MigrationResult(action="tier_1_pending",
                               reason=f"no_migrator_for_{target_category}")
    except Exception as e:
        return MigrationResult(action="failed", reason=str(e))
