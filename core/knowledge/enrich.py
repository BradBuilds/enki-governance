"""
AI Enrichment Pipeline.

Runs AFTER ingestion (async, best-effort). Takes a stored signal and:
  1. Generates a human-readable description
  2. Extracts key concepts
  3. Classifies epistemic type (fact/claim/opinion/recommendation)
  4. Generates semantic embedding (nomic-embed-text via Ollama)
  5. Deduplicates against existing nodes (concept overlap + embedding similarity)
  6. Finds connections to existing nodes
  7. Detects contrast points for claims/opinions

Never blocks ingestion. If AI is unavailable, data is still stored —
it just doesn't get enriched until the AI is configured.

Deduplication cascade (3 levels):
  Level 1 — Concept overlap: if 70%+ concepts match an existing node, link instead of create
  Level 2 — Embedding similarity: cosine > 0.85 = same entity, create edge
  Level 3 — LLM arbitration: for ambiguous cases (0.70-0.85), ask the model

Rate limits: max 5 new nodes + 10 edges per signal enrichment.
"""

import json
from typing import Optional

from core.ai.provider import BaseLLMProvider, LLMMessage
from core.knowledge.graph import (
    create_node, create_edge, find_by_concept, find_similar_nodes,
)

MAX_NODES_PER_SIGNAL = 5
MAX_EDGES_PER_SIGNAL = 10
CONCEPT_OVERLAP_THRESHOLD = 0.7
EMBEDDING_SIMILARITY_HIGH = 0.85
EMBEDDING_SIMILARITY_LOW = 0.70


ENRICHMENT_PROMPT = """You are analyzing data that was just ingested into a knowledge management system.

Given the signal data below, provide:
1. A concise human-readable DESCRIPTION (1-2 sentences)
2. A list of KEY CONCEPTS (3-7 single words or short phrases)
3. The EPISTEMIC TYPE: one of: fact, observation, claim, recommendation, opinion, question, definition
4. If this is a claim/recommendation/opinion, provide a CONTRAST (the opposing viewpoint in 1 sentence)

Respond in JSON:
{
  "description": "...",
  "concepts": ["concept1", "concept2", ...],
  "epistemic_type": "observation",
  "contrast": null or "The opposing view is..."
}

Signal data:"""


def enrich_signal(
    signal: dict,
    provider: BaseLLMProvider,
    project_id: str,
    tenant_id: str,
    embed_fn: Optional[callable] = None,
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """
    Enrich a signal with AI-generated metadata and add to knowledge graph.

    Args:
        signal: Signal dict (from get_signal or ingestion result)
        provider: Configured LLM provider
        project_id: Which project to add the node to
        tenant_id: Tenant scope
        embed_fn: Optional function(text) -> list[float] for embeddings
        dsn: Database DSN override

    Returns:
        Created knowledge node dict, or None if enrichment failed
    """
    if not provider or not provider.is_available():
        return None

    # Build prompt with signal data
    signal_summary = json.dumps({
        "signal_type": signal.get("signal_type"),
        "payload": signal.get("payload"),
        "timestamp": signal.get("timestamp"),
        "source": signal.get("source_domain"),
    }, default=str, indent=2)

    messages = [
        LLMMessage(role="system", content=ENRICHMENT_PROMPT),
        LLMMessage(role="user", content=signal_summary),
    ]

    response = provider.complete(messages, max_tokens=500, temperature=0.1)
    if response.error:
        return None

    # Parse AI response
    try:
        enrichment = _parse_enrichment_response(response.content)
    except Exception:
        return None

    concepts = enrichment.get("concepts", [])
    description = enrichment.get("description", "")

    # Generate embedding if embed function available
    embedding = None
    if embed_fn:
        embed_text = f"{description} {' '.join(concepts)}"
        try:
            embedding = embed_fn(embed_text)
        except Exception:
            pass  # Enrichment continues without embedding

    # ── Deduplication cascade ────────────────────────────────────────────────
    duplicate = _check_duplicate(
        project_id, tenant_id, concepts, embedding, provider, dsn,
    )

    if duplicate:
        # Link signal to existing node instead of creating a new one
        _link_signal_to_existing(
            duplicate["node_id"], signal.get("rid"), concepts, dsn,
        )
        return {"node_id": duplicate["node_id"], "deduplicated": True,
                "matched_by": duplicate["matched_by"]}

    # ── Create new knowledge node ────────────────────────────────────────────
    node = create_node(
        project_id=project_id,
        tenant_id=tenant_id,
        title=enrichment.get("description", signal.get("signal_type", "Untitled"))[:200],
        description=description,
        epistemic_type=enrichment.get("epistemic_type", "observation"),
        concepts=concepts,
        signal_rid=signal.get("rid"),
        source_authority="unverified",
        dsn=dsn,
    )

    if not node or node.get("error"):
        return None

    # Store embedding on the node
    if embedding:
        _store_embedding(node["node_id"], embedding, dsn)

    # Mark as AI-enriched
    _mark_enriched(node["node_id"], dsn)

    # Find connections to existing nodes via shared concepts
    edges_created = _connect_by_concepts(node, project_id, concepts, dsn)

    # Connect by embedding similarity (weaker links the dedup missed)
    if embedding and edges_created < MAX_EDGES_PER_SIGNAL:
        _connect_by_embedding(
            node, project_id, tenant_id, embedding,
            max_edges=MAX_EDGES_PER_SIGNAL - edges_created, dsn=dsn,
        )

    # If contrast detected, create a contrast node
    contrast = enrichment.get("contrast")
    if contrast and enrichment.get("epistemic_type") in ("claim", "recommendation", "opinion"):
        _create_contrast_node(node, contrast, project_id, tenant_id, dsn)

    return node


def _parse_enrichment_response(text: str) -> dict:
    """Parse the AI's JSON response, handling markdown code blocks."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    return json.loads(text)


def _check_duplicate(
    project_id: str,
    tenant_id: str,
    concepts: list[str],
    embedding: Optional[list[float]],
    provider: Optional[BaseLLMProvider],
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """
    3-level deduplication cascade.

    Returns matching node dict with 'matched_by' key, or None if no duplicate.
    """
    # Level 1: concept overlap
    match = _check_concept_overlap(project_id, concepts, dsn)
    if match:
        return match

    # Level 2: embedding similarity (high confidence)
    if embedding:
        similar = find_similar_nodes(
            project_id, tenant_id, embedding,
            threshold=EMBEDDING_SIMILARITY_HIGH, limit=1, dsn=dsn,
        )
        if similar:
            return {"node_id": similar[0]["node_id"], "matched_by": "embedding",
                    "similarity": similar[0]["similarity"]}

    # Level 3: LLM arbitration for ambiguous embedding matches
    if embedding and provider:
        ambiguous = find_similar_nodes(
            project_id, tenant_id, embedding,
            threshold=EMBEDDING_SIMILARITY_LOW, limit=3, dsn=dsn,
        )
        # Filter out high-confidence matches (already checked)
        ambiguous = [n for n in ambiguous if n["similarity"] < EMBEDDING_SIMILARITY_HIGH]
        if ambiguous:
            match = _llm_dedup_check(concepts, ambiguous, provider)
            if match:
                return match

    return None


def _check_concept_overlap(
    project_id: str,
    concepts: list[str],
    dsn: Optional[str] = None,
) -> Optional[dict]:
    """Level 1: check if 70%+ of concepts match an existing node."""
    if not concepts:
        return None

    from storage.postgres.db import get_connection
    normalized = [c.lower().strip() for c in concepts]

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ci.node_id, COUNT(*) as overlap
                FROM concept_index ci
                WHERE ci.project_id = %s AND ci.concept = ANY(%s)
                GROUP BY ci.node_id
                HAVING COUNT(*) >= 2
                ORDER BY overlap DESC
                LIMIT 5
            """, (project_id, normalized))

            for row in cur.fetchall():
                overlap_ratio = row[1] / len(concepts)
                if overlap_ratio >= CONCEPT_OVERLAP_THRESHOLD:
                    return {"node_id": row[0], "matched_by": "concept_overlap",
                            "overlap_ratio": overlap_ratio}
    return None


def _llm_dedup_check(
    concepts: list[str],
    candidates: list[dict],
    provider: BaseLLMProvider,
) -> Optional[dict]:
    """Level 3: ask LLM if ambiguous candidates are duplicates."""
    candidate_text = "\n".join(
        f"- Node {c['node_id']}: {c['title']} (concepts: {', '.join(c.get('concepts') or [])})"
        for c in candidates
    )

    messages = [
        LLMMessage(role="system", content=(
            "You are deduplicating knowledge graph nodes. "
            "Given new concepts and existing candidate nodes, determine if any candidate "
            "describes the SAME thing. Respond with ONLY the node_id if it's a match, "
            "or 'NONE' if no match."
        )),
        LLMMessage(role="user", content=(
            f"New node concepts: {', '.join(concepts)}\n\n"
            f"Existing candidates:\n{candidate_text}\n\n"
            f"Is any candidate the same entity? Reply with node_id or NONE."
        )),
    ]

    response = provider.complete(messages, max_tokens=50, temperature=0.0)
    if response.error:
        return None

    answer = response.content.strip()
    for c in candidates:
        if c["node_id"] in answer:
            return {"node_id": c["node_id"], "matched_by": "llm_arbitration",
                    "similarity": c["similarity"]}
    return None


def _link_signal_to_existing(
    node_id: str,
    signal_rid: Optional[int],
    new_concepts: list[str],
    dsn: Optional[str] = None,
):
    """Link a new signal to an existing node and merge concepts."""
    from storage.postgres.db import get_connection
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Add new concepts to existing node
            if new_concepts:
                normalized = [c.lower().strip() for c in new_concepts]
                cur.execute("""
                    UPDATE knowledge_nodes
                    SET concepts = (
                        SELECT ARRAY(SELECT DISTINCT unnest(concepts || %s))
                    ), updated_at = NOW()
                    WHERE node_id = %s
                """, (normalized, node_id))

                # Update concept index
                for concept in normalized:
                    # Need project_id for concept_index
                    cur.execute("""
                        INSERT INTO concept_index (concept, node_id, project_id)
                        SELECT %s, %s, project_id FROM knowledge_nodes WHERE node_id = %s
                        ON CONFLICT DO NOTHING
                    """, (concept, node_id, node_id))

            # Store signal reference in metadata
            if signal_rid:
                cur.execute("""
                    UPDATE knowledge_nodes
                    SET metadata = jsonb_set(
                        COALESCE(metadata, '{}'::jsonb),
                        '{additional_signal_rids}',
                        COALESCE(metadata->'additional_signal_rids', '[]'::jsonb) || %s::jsonb
                    ), updated_at = NOW()
                    WHERE node_id = %s
                """, (json.dumps(signal_rid), node_id))

            conn.commit()


def _store_embedding(
    node_id: str,
    embedding: list[float],
    dsn: Optional[str] = None,
):
    """Store a vector embedding on a knowledge node."""
    from storage.postgres.db import get_connection
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE knowledge_nodes SET embedding = %s::vector, updated_at = NOW()
                WHERE node_id = %s
            """, (str(embedding), node_id))
        conn.commit()


def _mark_enriched(node_id: str, dsn: Optional[str] = None):
    """Mark a node as AI-enriched."""
    from storage.postgres.db import get_connection
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE knowledge_nodes SET ai_enriched = TRUE, updated_at = NOW()
                WHERE node_id = %s
            """, (node_id,))
        conn.commit()


def _connect_by_concepts(
    node: dict,
    project_id: str,
    concepts: list[str],
    dsn: Optional[str] = None,
) -> int:
    """Find existing nodes with shared concepts and create edges. Returns edge count."""
    node_id = node["node_id"]
    edges_created = 0

    for concept in concepts:
        if edges_created >= MAX_EDGES_PER_SIGNAL:
            break
        related = find_by_concept(project_id, concept, dsn)
        for related_node in related:
            if edges_created >= MAX_EDGES_PER_SIGNAL:
                break
            if related_node["node_id"] == node_id:
                continue
            result = create_edge(
                project_id=project_id,
                source_node_id=node_id,
                target_node_id=related_node["node_id"],
                relationship="same_concept",
                strength=0.7,
                evidence=f"Shared concept: {concept}",
                ai_generated=True,
                dsn=dsn,
            )
            if result.get("created"):
                edges_created += 1

    return edges_created


def _connect_by_embedding(
    node: dict,
    project_id: str,
    tenant_id: str,
    embedding: list[float],
    max_edges: int = 5,
    dsn: Optional[str] = None,
):
    """Connect to semantically similar nodes found via embedding."""
    similar = find_similar_nodes(
        project_id, tenant_id, embedding,
        threshold=0.6,  # Lower threshold for "related" links
        limit=max_edges,
        exclude_node_id=node["node_id"],
        dsn=dsn,
    )

    for s in similar:
        create_edge(
            project_id=project_id,
            source_node_id=node["node_id"],
            target_node_id=s["node_id"],
            relationship="references",
            strength=round(s["similarity"], 2),
            evidence=f"Semantic similarity: {s['similarity']:.2f}",
            ai_generated=True,
            dsn=dsn,
        )


def _create_contrast_node(
    source_node: dict,
    contrast_text: str,
    project_id: str,
    tenant_id: str,
    dsn: Optional[str] = None,
):
    """Create a contrast node linked to the source node."""
    contrast_node = create_node(
        project_id=project_id,
        tenant_id=tenant_id,
        title=f"Contrast: {contrast_text[:100]}",
        description=contrast_text,
        epistemic_type="claim",
        concepts=[],
        parent_node_id=source_node["node_id"],
        source_authority="unverified",
        dsn=dsn,
    )

    if contrast_node and not contrast_node.get("error"):
        create_edge(
            project_id=project_id,
            source_node_id=source_node["node_id"],
            target_node_id=contrast_node["node_id"],
            relationship="contrasts",
            strength=0.8,
            evidence="AI-detected opposing viewpoint",
            ai_generated=True,
            dsn=dsn,
        )
