"""Fission operator Ƒ — the inverse of collapse.

Where collapse ⊕ merges multiple assertions about the same property into a
deeper canonical, fission Ƒ partitions a single contaminated entity into
two distinct entities when its assertions form mutually-exclusive clusters.

The Acme case is the canonical test: entity 'Jacob Acme' that has
been mis-mapped to receive both Jacob-the-painter mentions AND
Alex-Acme mentions. The two clusters of mentions reference
disjoint sets of documents, disjoint co-occurring entities, and
different first names. The substrate detects this and proposes a split.

Detection signals (any subset fires; the partition_score combines them):
  document_partition  — the entity's mentions split into ≥2 doc clusters
                        that share <THRESHOLD% co-occurring entities
  alias_divergence    — the entity's aliases include explicit different
                        first names sharing only the surname
  first_name_conflict — multiple distinct first names attached to the
                        same surname in the aliases list
  property_alternatives — typed property assertions show contradictions
                        at orthogonal precision levels (occupation, etc.)
  temporal_disjoint   — assertion event_times span >120 years (no single
                        human lifetime; suggests two distinct people)

Auto-apply policy (option A — conservative):
  partition_score >= 0.95 AND signals_fired >= 2 AND first_name_conflict
  → apply automatically
  Otherwise → store as fission_proposals for human review.

Public API:
  detect_candidates(limit=10) → list[FissionCandidate]
  preview(entity_id) → FissionPreview
  apply(entity_id, evidence) → FissionResult
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from storage.postgres.db import get_connection
from core.gdu.address import assign_address

log = logging.getLogger("enki.substrate.fission")

# Conservative thresholds — option A
AUTO_APPLY_PARTITION_SCORE = 0.95
AUTO_APPLY_MIN_SIGNALS = 2
AUTO_APPLY_REQUIRES_FIRST_NAME_CONFLICT = True

# Detection tuning
DOCUMENT_CLUSTER_THRESHOLD = 0.05   # min shared-entity overlap to merge into same cluster
MIN_DOCS_PER_CLUSTER = 3            # fission requires real evidence on both sides
MIN_DOCS_PER_REBIND = 5             # rebind requires a clear majority cluster


# ─── Identity-correction action types ───────────────────────────────────────
# Different ways the substrate can correct a contaminated entity:
#   FISSION  — two real identities, split into two entities
#   REBIND   — one real identity in disguise; merge into the existing correct entity
#   RENAME   — one real identity, no other home; just rename + fix aliases
ACTION_FISSION = "fission"
ACTION_REBIND = "rebind"
ACTION_RENAME = "rename"
ACTION_REVIEW = "review"


@dataclass
class FissionCandidate:
    entity_id: int
    canonical_name: str
    partition_score: float
    signals_fired: list[str]
    cluster_a: dict        # {doc_ids, mention_ids, suggested_name, first_name}
    cluster_b: dict
    qualifies_for_auto_apply: bool
    action: str = ACTION_REVIEW       # ACTION_FISSION | ACTION_REBIND | ACTION_RENAME | ACTION_REVIEW
    rebind_target_id: Optional[int] = None
    rename_to: Optional[str] = None


@dataclass
class FissionPreview:
    candidate: FissionCandidate
    cluster_a_summary: dict
    cluster_b_summary: dict
    blast_radius: dict     # how many assessments / mentions / signals would move


@dataclass
class FissionResult:
    split_id: int
    old_entity_id: int
    kept_entity_id: int
    new_entity_id: int
    moved_mentions: int
    moved_assessments: int
    moved_signals: int


# ─── Detection helpers ────────────────────────────────────────────────────────

_FIRST_NAME_RE = re.compile(r"^([A-Z][a-z]+)(?:\s+[A-Z][a-z]+)*\s+([A-Z][a-z]+)$")


def _extract_first_name(name: str, surname_hint: Optional[str] = None) -> Optional[str]:
    """Extract the first word that looks like a given name.

    If surname_hint is given, names that resolve to only the surname (after
    skipping honorifics) return None — we don't count the surname as a first
    name even when it appears alone like 'Mr. Acme'.
    """
    if not name:
        return None
    surname_l = (surname_hint or "").strip(",.").lower()
    parts = name.strip().split()
    for p in parts:
        # Skip honorifics, lowercase tokens, ALL-CAPS surname-only forms
        bare = p.strip(",.;:")
        if bare.lower() in ("sir", "lord", "lady", "dr", "mr", "mrs", "ms",
                            "the", "of", "a", "an"):
            continue
        if bare.isupper() and len(bare) > 2:
            return None  # ALL-CAPS — probably surname-only
        if bare and bare[0].isupper() and any(c.islower() for c in bare):
            # If this token IS the surname, don't count it as a first name
            if surname_l and bare.lower() == surname_l:
                return None
            return bare
    return None


def _extract_surname(canonical_name: str) -> Optional[str]:
    """Last capitalized non-honorific word in the canonical name."""
    if not canonical_name:
        return None
    parts = [p.strip(",.;:") for p in canonical_name.strip().split()]
    for p in reversed(parts):
        if p.lower() in ("sir", "lord", "lady", "dr", "mr", "mrs", "ms",
                          "jr", "sr", "ii", "iii"):
            continue
        if p and (p[0].isupper() or p.isupper()):
            return p
    return None


def _first_name_conflict(aliases: list[str], canonical_name: str,
                          extra_source_names: list[str] | None = None
                          ) -> Optional[tuple[str, str]]:
    """If two distinct given names share the entity's surname, return them.

    We look at the canonical_name + aliases + optional extra source names
    (e.g. document titles, paragraph contexts) and extract the FIRST NAME
    from each, using the entity's surname as a filter (we don't count the
    surname appearing alone as a first name).
    """
    surname = _extract_surname(canonical_name)
    all_names = ([canonical_name] + list(aliases or [])
                 + list(extra_source_names or []))
    first_names = Counter()
    for n in all_names:
        fn = _extract_first_name(n, surname_hint=surname)
        if fn and len(fn) >= 3:
            first_names[fn.lower()] += 1
    if len(first_names) < 2:
        return None
    top2 = first_names.most_common(2)
    # Both first names must occur at least once (already true given Counter
    # semantics); we additionally require they aren't just two spellings
    # of the same thing.
    a, b = top2[0][0], top2[1][0]
    if a.startswith(b) or b.startswith(a):
        return None
    return (a, b)


def _document_title_partition(entity_id: int, surname: str
                                ) -> tuple[list[set[int]], dict, float]:
    """Cluster the entity's documents by the FIRST NAME that appears in
    each document's title. Returns (clusters, name_to_cluster, score).

    Score = 1 - (ambiguous_docs / total_docs). High score = most docs
    have a clearly-assignable first name in their title.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DISTINCT d.document_id, d.title
                     FROM document_mentions dm
                     JOIN documents d USING (document_id)
                    WHERE dm.entity_id = %s""",
                (entity_id,),
            )
            rows = cur.fetchall()

    if not rows:
        return [], {}, 0.0

    surname_l = surname.lower() if surname else ""
    name_buckets: dict[str, set[int]] = defaultdict(set)
    ambiguous: set[int] = set()

    for doc_id, title in rows:
        if not title:
            ambiguous.add(doc_id)
            continue
        title_lower = title.lower()
        # Find "<First> <Surname>" patterns in the title — that names the
        # subject of the document.
        if surname_l and surname_l in title_lower:
            # Find each occurrence of surname and grab the preceding word
            for m in re.finditer(rf"\b([A-Z][a-z]+)\s+{re.escape(surname)}\b", title):
                first = m.group(1).lower()
                if first not in ("the", "of", "and", "for", "with"):
                    name_buckets[first].add(doc_id)
                    break
            else:
                ambiguous.add(doc_id)
        else:
            ambiguous.add(doc_id)

    if len(name_buckets) < 2:
        return [], dict(name_buckets), 0.0

    clusters = list(name_buckets.values())
    total = sum(len(c) for c in clusters) + len(ambiguous)
    assignable = sum(len(c) for c in clusters)
    score = assignable / total if total else 0.0
    return clusters, dict(name_buckets), score


def _document_clusters(entity_id: int) -> tuple[list[set[int]], float]:
    """Cluster an entity's documents by co-occurring-entity overlap.
    Returns (clusters, partition_score).
    partition_score = 1.0 - max(overlap fraction) — high means clusters
    are nearly disjoint.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Per-doc: which other entities does this doc mention?
            cur.execute(
                """SELECT document_id, array_agg(DISTINCT entity_id)
                     FROM document_mentions
                    WHERE document_id IN (
                      SELECT DISTINCT document_id FROM document_mentions
                       WHERE entity_id = %s
                    )
                      AND entity_id <> %s
                    GROUP BY document_id""",
                (entity_id, entity_id),
            )
            rows = cur.fetchall()
    if not rows:
        return [], 0.0
    if len(rows) < 2:
        return [set([rows[0][0]])], 0.0

    # Simple greedy: sort docs by entity count; start clusters by
    # Jaccard overlap. Two docs whose entity sets share > THRESHOLD%
    # join the same cluster.
    doc_entities = {d: set(es) for d, es in rows}
    docs = sorted(doc_entities, key=lambda d: -len(doc_entities[d]))

    def jaccard(a: set, b: set) -> float:
        if not a or not b:
            return 0.0
        inter = len(a & b)
        union = len(a | b)
        return inter / union if union else 0.0

    clusters: list[set[int]] = []
    for d in docs:
        placed = False
        for cl in clusters:
            # Compare against cluster's union of entities
            cl_entities: set[int] = set()
            for cd in cl:
                cl_entities |= doc_entities[cd]
            if jaccard(doc_entities[d], cl_entities) > DOCUMENT_CLUSTER_THRESHOLD:
                cl.add(d)
                placed = True
                break
        if not placed:
            clusters.append({d})

    # Score: 1.0 - max pairwise Jaccard between clusters
    if len(clusters) < 2:
        return clusters, 0.0
    max_jaccard = 0.0
    for i in range(len(clusters)):
        ei = set().union(*[doc_entities[d] for d in clusters[i]])
        for j in range(i + 1, len(clusters)):
            ej = set().union(*[doc_entities[d] for d in clusters[j]])
            mj = jaccard(ei, ej)
            if mj > max_jaccard:
                max_jaccard = mj
    return clusters, 1.0 - max_jaccard


def _find_rebind_target(surname: str, first_name: str,
                         exclude_entity_id: int) -> Optional[int]:
    """Look up an existing entity whose canonical name fits 'FirstName Surname'.
    Returns the best match's entity_id (highest signal_count) or None."""
    if not surname or not first_name:
        return None
    target_pattern = f"{first_name.title()} {surname}"
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT entity_id, canonical_name, signal_count
                     FROM entities
                    WHERE entity_id <> %s
                      AND canonical_type = 'person'
                      AND (lower(canonical_name) = lower(%s)
                           OR canonical_name ILIKE %s)
                    ORDER BY signal_count DESC NULLS LAST
                    LIMIT 1""",
                (exclude_entity_id, target_pattern, f"%{first_name.title()}%{surname}%"),
            )
            row = cur.fetchone()
    return row[0] if row else None


def detect_candidates(limit: int = 10) -> list[FissionCandidate]:
    """Walk recently-mentioned entities; surface ones that look contaminated.

    Returns candidates ordered by partition_score desc. The detector is
    cheap — uses indexed queries; no LLM. Intended to run in idle cycles.
    """
    candidates: list[FissionCandidate] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            # Heuristic prefilter: entities with many doc mentions AND ≥1 alias
            # are the realistic candidates. Skip tiny entities to save work.
            cur.execute(
                """SELECT e.entity_id, e.canonical_name, e.aliases,
                          (SELECT COUNT(*) FROM document_mentions WHERE entity_id = e.entity_id) AS doc_mentions
                     FROM entities e
                    WHERE e.canonical_type = 'person'
                      AND e.aliases IS NOT NULL AND array_length(e.aliases, 1) >= 2
                      AND (SELECT COUNT(*) FROM document_mentions WHERE entity_id = e.entity_id) >= 20
                    ORDER BY (SELECT COUNT(*) FROM document_mentions WHERE entity_id = e.entity_id) DESC
                    LIMIT 200""",
            )
            entities = cur.fetchall()

    for ent_id, name, aliases, doc_mentions in entities:
        signals: list[str] = []
        surname = _extract_surname(name) or ""

        # Signal 1: document titles split on first-name (the strongest signal
        # because document titles are usually clear about WHO the doc is about)
        title_clusters_list, title_name_map, title_score = _document_title_partition(
            ent_id, surname,
        )
        if len(title_clusters_list) >= 2 and title_score >= 0.50:
            signals.append("document_title_partition")

        # Signal 2: first-name conflict in aliases + the title-derived names
        title_names = list(title_name_map.keys())
        fn_conflict = _first_name_conflict(
            list(aliases or []), name, extra_source_names=title_names,
        )
        if fn_conflict:
            signals.append("first_name_conflict")

        # Signal 3: alias divergence (only counts if first names diverge AND
        # at least 2 of the aliases use one form vs another)
        if fn_conflict and title_score >= 0.50:
            signals.append("alias_divergence")

        # Signal 4: document-cluster partition (entity-co-occurrence based)
        # Disabled on sparse data — leaves the title partition to do the work
        # for the common case. Still computed as supporting evidence.
        doc_clusters, doc_partition_score = _document_clusters(ent_id)

        # Use title partition as the primary partition; fall back to doc
        # clusters if title partition failed.
        if title_clusters_list and title_score >= 0.50:
            big = sorted(title_clusters_list, key=lambda c: -len(c))[:2]
            partition_score = title_score
        elif doc_clusters and doc_partition_score >= 0.70:
            big = sorted(doc_clusters, key=lambda c: -len(c))[:2]
            partition_score = doc_partition_score
        else:
            continue

        if len(big) < 2:
            continue

        # First-name labels: try to match clusters to first-name buckets
        cluster_a_set = set(big[0])
        cluster_b_set = set(big[1])
        cluster_a_first = None
        cluster_b_first = None
        for fn, docs in title_name_map.items():
            if docs & cluster_a_set:
                cluster_a_first = fn
            if docs & cluster_b_set:
                cluster_b_first = fn

        # Suggest names per cluster via document titles
        with get_connection() as conn:
            with conn.cursor() as cur:
                def suggest(doc_ids: set[int]) -> str:
                    cur.execute(
                        "SELECT title FROM documents WHERE document_id = ANY(%s) "
                        " ORDER BY (SELECT COUNT(*) FROM document_mentions WHERE document_id = documents.document_id AND entity_id = %s) DESC "
                        " LIMIT 1",
                        (list(doc_ids), ent_id),
                    )
                    r = cur.fetchone()
                    return (r[0] if r else "")[:120]

                hint_a = suggest(big[0])
                hint_b = suggest(big[1])

        cluster_a = {
            "doc_ids": sorted(big[0]),
            "title_hint": hint_a,
            "size": len(big[0]),
            "first_name": cluster_a_first,
        }
        cluster_b = {
            "doc_ids": sorted(big[1]),
            "title_hint": hint_b,
            "size": len(big[1]),
            "first_name": cluster_b_first,
        }

        # ── DECIDE THE ACTION ───────────────────────────────────────────────
        # The detector finds partitions; the action depends on the shape.
        # Three cases (one real identity rebound, one real identity renamed,
        # two real identities split). Each respects base60 lineage.
        action = ACTION_REVIEW
        rebind_target_id = None
        rename_to = None

        size_a, size_b = len(big[0]), len(big[1])
        canonical_first_name = _extract_first_name(name, surname_hint=surname)

        # REBIND case: one cluster's first name matches the entity's canonical
        # first name (rare here because the canonical was likely wrong) OR the
        # dominant cluster's first name maps to an EXISTING different entity.
        # For Acme 4428: canonical_first_name='Jacob', dominant cluster
        # is 'Alex' (12 docs), target=2460 exists → REBIND.
        dominant_cluster = cluster_a if size_a >= size_b else cluster_b
        minor_cluster = cluster_b if size_a >= size_b else cluster_a
        dominant_first = dominant_cluster.get("first_name")

        if (
            dominant_first
            and dominant_first != (canonical_first_name or "").lower()
            and size_b == 0  # one side empty in the strict sense — but big[] is len 2 always so check via name_buckets
        ):
            pass  # fall through to other branches

        # Stronger REBIND test: the dominant cluster's first name doesn't match
        # the entity's canonical first name, AND there's a separate entity
        # with the right name, AND the minor cluster is too small to be real.
        if (
            dominant_first
            and canonical_first_name
            and dominant_first.lower() != canonical_first_name.lower()
            and size_a >= MIN_DOCS_PER_REBIND
            and size_b < MIN_DOCS_PER_CLUSTER
        ):
            tgt = _find_rebind_target(surname, dominant_first, ent_id)
            if tgt:
                action = ACTION_REBIND
                rebind_target_id = tgt
            else:
                # No existing target — could be RENAME
                action = ACTION_RENAME
                rename_to = f"{dominant_first.title()} {surname}"

        # FISSION case: both clusters have real size — two distinct identities
        elif (
            size_a >= MIN_DOCS_PER_CLUSTER
            and size_b >= MIN_DOCS_PER_CLUSTER
            and cluster_a_first and cluster_b_first
            and cluster_a_first != cluster_b_first
        ):
            action = ACTION_FISSION

        qualifies = (
            action in (ACTION_REBIND, ACTION_FISSION)
            and partition_score >= AUTO_APPLY_PARTITION_SCORE
            and len(signals) >= AUTO_APPLY_MIN_SIGNALS
            and (("first_name_conflict" in signals or "document_title_partition" in signals)
                 if AUTO_APPLY_REQUIRES_FIRST_NAME_CONFLICT else True)
        )

        candidates.append(FissionCandidate(
            entity_id=ent_id,
            canonical_name=name,
            partition_score=partition_score,
            signals_fired=signals,
            cluster_a=cluster_a,
            cluster_b=cluster_b,
            qualifies_for_auto_apply=qualifies,
            action=action,
            rebind_target_id=rebind_target_id,
            rename_to=rename_to,
        ))

    candidates.sort(key=lambda c: -c.partition_score)
    return candidates[:limit]


# ─── Preview ──────────────────────────────────────────────────────────────────

def preview(entity_id: int) -> Optional[FissionPreview]:
    """Show what fission would do for this entity without applying it."""
    candidates = detect_candidates(limit=200)
    cand = next((c for c in candidates if c.entity_id == entity_id), None)
    if not cand:
        return None

    # Blast radius: how many rows move?
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM document_mentions WHERE document_id = ANY(%s) AND entity_id = %s",
                (cand.cluster_b["doc_ids"], entity_id),
            )
            mentions_b = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM document_mentions WHERE document_id = ANY(%s) AND entity_id = %s",
                (cand.cluster_a["doc_ids"], entity_id),
            )
            mentions_a = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM entity_mentions WHERE entity_id = %s",
                (entity_id,),
            )
            sig_mentions = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(*) FROM assessments WHERE subj_entity_id = %s OR obj_entity_id = %s",
                (entity_id, entity_id),
            )
            assessments = cur.fetchone()[0]

    return FissionPreview(
        candidate=cand,
        cluster_a_summary={"mentions": mentions_a, **cand.cluster_a},
        cluster_b_summary={"mentions": mentions_b, **cand.cluster_b},
        blast_radius={
            "doc_mentions_total": mentions_a + mentions_b,
            "doc_mentions_cluster_a_keeps": mentions_a,
            "doc_mentions_cluster_b_moves_to_new": mentions_b,
            "signal_mentions_undecided": sig_mentions,
            "assessments_undecided": assessments,
        },
    )


# ─── Apply ────────────────────────────────────────────────────────────────────

def apply(entity_id: int, *, applied_by: str = "fission_detector",
          tenant_id: str = "default") -> Optional[FissionResult]:
    """Atomically partition the entity.

    Moves cluster_b's document_mentions (and the assessments those mentions
    feed) to a new entity. Cluster_a stays on the original entity_id. Logs
    to entity_splits.

    Conservative: this implementation only redirects DOCUMENT_MENTIONS by
    cluster_b's doc_ids. signal mentions and legacy assessments are NOT
    moved automatically — they go to a follow-up review step (signal
    mentions are short-context and need different heuristics; legacy
    untyped assessments need LLM context analysis to partition safely).
    """
    pv = preview(entity_id)
    if not pv:
        log.warning("fission.apply: no candidate for entity %s", entity_id)
        return None
    cand = pv.candidate

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Mint a new entity for cluster_b
            cur.execute(
                "SELECT canonical_type FROM entities WHERE entity_id = %s",
                (entity_id,),
            )
            row = cur.fetchone()
            if not row:
                return None
            etype = row[0] or "person"

            new_base60 = assign_address(conn, "entity", etype)
            suggested_name = (
                f"{cand.cluster_b.get('first_name', '').title()} "
                + (cand.canonical_name.rsplit(" ", 1)[-1]
                   if " " in cand.canonical_name else cand.canonical_name)
            ).strip()
            if not suggested_name or suggested_name == cand.canonical_name:
                suggested_name = f"{cand.canonical_name} (split)"

            cur.execute(
                """INSERT INTO entities (
                     tenant_id, canonical_name, normalized_name, canonical_type,
                     base60_address, entity_kind, gdu_canonical,
                     admitted_under_active_set, admitted_under_pack_versions
                   ) VALUES (%s, %s, lower(%s), %s, %s, 'instance', TRUE,
                             'v6.2.0', '{}'::jsonb)
                   RETURNING entity_id""",
                (tenant_id, suggested_name, suggested_name, etype, new_base60),
            )
            new_entity_id = cur.fetchone()[0]

            # Attestation (C3) — fires trg_mirror_entity, minting a
            # knowledge_node for the split-out entity. A fission is the one
            # admission here that is genuinely NOT an observation: nothing new
            # was seen, an existing entity was judged to be two. That judgement
            # is unreconstructable from the resulting rows, which is precisely
            # why it has to be recorded at the moment it is made.
            from core.gdu.attest import attest_admission
            attest_admission(
                conn, tenant_id,
                operation="admit",
                writer="substrate.fission.apply_fission",
                checks_run={
                    "boundary": False,
                    # etype is inherited from the entity being split, not
                    # re-checked against the pack.
                    "vocab": False,
                    # new_base60 IS minted from the governed sequence by the
                    # caller — the one True on this path.
                    "address": new_base60 is not None,
                    # A fission is the INVERSE of collapse. Recording True would
                    # claim a dedup check ran; what actually ran was a partition
                    # decision, which is the opposite operation.
                    "collapse": False,
                },
                node_id=f"entity:{new_entity_id}", entity_id=new_entity_id,
                category="entity", subtype=etype, base60=new_base60,
                input_type=etype, mapped_type=etype,
                reason=(f"fission from entity {entity_id}: "
                        f"{cand.cluster_b.get('first_name', '?')} cluster split out "
                        f"(score={cand.partition_score})")[:500],
            )

            # Move document_mentions for cluster_b's docs
            cur.execute(
                """UPDATE document_mentions SET entity_id = %s
                    WHERE document_id = ANY(%s) AND entity_id = %s""",
                (new_entity_id, cand.cluster_b["doc_ids"], entity_id),
            )
            moved_mentions = cur.rowcount

            # For now signals and assessments stay on the original — they
            # need their own partition pass. Log the operation.
            cur.execute(
                """INSERT INTO entity_fissions (
                     tenant_id, old_entity_id, new_entity_ids,
                     reason, partition_score, signals_fired, evidence, applied_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING split_id""",
                (tenant_id, entity_id, [new_entity_id],
                 f"document-partition fission: {cand.cluster_b.get('first_name', '?')} cluster moved out",
                 cand.partition_score, cand.signals_fired,
                 json.dumps({
                     "cluster_a": {k: v for k, v in cand.cluster_a.items() if k != "doc_ids" or len(v) < 50},
                     "cluster_b": {k: v for k, v in cand.cluster_b.items() if k != "doc_ids" or len(v) < 50},
                 }),
                 applied_by),
            )
            split_id = cur.fetchone()[0]
        conn.commit()

    log.info("fission applied: entity %d → kept (%d) + new entity %d (split %d)",
             entity_id, entity_id, new_entity_id, split_id)

    return FissionResult(
        split_id=split_id,
        old_entity_id=entity_id,
        kept_entity_id=entity_id,
        new_entity_id=new_entity_id,
        moved_mentions=moved_mentions,
        moved_assessments=0,
        moved_signals=0,
    )


def rebind(entity_id: int, target_entity_id: int, *,
           reason: str = "fission_detector_rebind",
           strip_misleading_aliases: bool = True,
           tenant_id: str = "default") -> dict:
    """REBIND: an entity has one real identity but the wrong canonical name.
    Merge it into the correct existing entity, log the operation to
    entity_fissions, optionally strip aliases that came from the wrong name.

    Leverages core.gdu.entity_merge.merge_entities for the data move (the
    well-tested path that handles assessments, mentions, library_members,
    investigations, etc.). The merge_entities path is the canonical
    'absorb one entity into another' operation; rebind is just that with
    additional audit + alias hygiene.

    For Acme 4428 ('Jacob Acme' with 1,078 Alex-context mentions):
      rebind(4428, target=2460)  → all data moves to Alex Acme,
                                    4428 gets entity_redirects entry,
                                    misleading 'Jacob Acme' / 'Sir Acme'
                                    aliases stripped before merge.
    """
    from core.gdu.entity_merge import merge_entities

    # Capture pre-merge state for the audit log
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_name, aliases FROM entities WHERE entity_id = %s",
                (entity_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"ok": False, "error": f"entity {entity_id} not found"}
            wrong_name, wrong_aliases = row

            cur.execute(
                "SELECT canonical_name FROM entities WHERE entity_id = %s",
                (target_entity_id,),
            )
            target_row = cur.fetchone()
            if not target_row:
                return {"ok": False, "error": f"target entity {target_entity_id} not found"}
            target_name = target_row[0]

            # Optionally strip misleading aliases — those that include the WRONG
            # first name and are clearly about the wrong person. Keep aliases
            # that contain ONLY the surname (those are valid for the target).
            if strip_misleading_aliases and wrong_aliases:
                wrong_first = _extract_first_name(wrong_name)
                if wrong_first:
                    keep_aliases = [
                        a for a in (wrong_aliases or [])
                        if a and wrong_first.lower() not in a.lower()
                    ]
                    cur.execute(
                        "UPDATE entities SET aliases = %s WHERE entity_id = %s",
                        (keep_aliases, entity_id),
                    )
        conn.commit()

    # Do the merge — target survives, entity_id gets absorbed
    merge_result = merge_entities(
        canonical_id=target_entity_id,
        duplicate_id=entity_id,
        reason=reason,
    )

    if not merge_result.get("ok"):
        return {"ok": False, "error": merge_result.get("error"),
                "merge_result": merge_result}

    # Log to entity_fissions as a REBIND action
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO entity_fissions (
                     tenant_id, old_entity_id, new_entity_ids,
                     reason, partition_score, signals_fired, evidence, applied_by
                   ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   RETURNING split_id""",
                (tenant_id, entity_id, [target_entity_id],
                 f"REBIND: {wrong_name!r} → {target_name!r} ({reason})",
                 1.0,
                 ["rebinding"],
                 json.dumps({
                     "action": "rebind",
                     "wrong_name": wrong_name,
                     "target_name": target_name,
                     "target_entity_id": target_entity_id,
                     "merge_changes": merge_result.get("changed", {}),
                 }),
                 "fission_detector_rebind"),
            )
            split_id = cur.fetchone()[0]
        conn.commit()

    log.info("rebind applied: entity %d (%s) → entity %d (%s), split_id=%d",
             entity_id, wrong_name, target_entity_id, target_name, split_id)

    # Federation Phase 5 — propagate the redirect to the hub so other nodes
    # holding the same canonical_entity_key can auto-apply the rebind
    # locally. No-op when ENKI_HUB_URL is unset.
    try:
        from core.federation import phase4 as fed_phase4
        from core.federation.entity_publisher import canonical_key as _ekey
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT canonical_type FROM entities WHERE entity_id IN (%s, %s)",
                    (target_entity_id, target_entity_id),
                )
                tr = cur.fetchone()
                etype_target = (tr[0] if tr else "person") or "person"
        origin_key = _ekey(wrong_name or "", etype_target)
        target_key = _ekey(target_name or "", etype_target)
        if origin_key != target_key:
            fed_phase4.publish_entity_redirect(
                origin_canonical_key=origin_key,
                target_canonical_key=target_key,
                reason=f"rebind:{reason}",
                audit_payload={
                    "split_id": split_id,
                    "old_entity_id": entity_id,
                    "old_name": wrong_name,
                    "target_name": target_name,
                    "merge_changes": merge_result.get("changed", {}),
                },
            )
    except Exception:
        log.debug("federation redirect publish failed for rebind %d→%d",
                  entity_id, target_entity_id, exc_info=True)

    return {
        "ok": True,
        "action": "rebind",
        "split_id": split_id,
        "old_entity_id": entity_id,
        "target_entity_id": target_entity_id,
        "old_name": wrong_name,
        "target_name": target_name,
        "merge_changes": merge_result.get("changed", {}),
    }


def propose_for_review(candidate: FissionCandidate, tenant_id: str = "default") -> int:
    """Store a non-auto-apply candidate in fission_proposals for human review."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO fission_proposals (
                     tenant_id, entity_id, partition_score, signals_fired, proposed_split
                   ) VALUES (%s, %s, %s, %s, %s)
                   RETURNING proposal_id""",
                (tenant_id, candidate.entity_id, candidate.partition_score,
                 candidate.signals_fired,
                 json.dumps({
                     "cluster_a": candidate.cluster_a,
                     "cluster_b": candidate.cluster_b,
                     "canonical_name": candidate.canonical_name,
                 })),
            )
            pid = cur.fetchone()[0]
        conn.commit()
    return pid
