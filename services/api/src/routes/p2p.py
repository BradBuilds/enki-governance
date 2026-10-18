"""
P2P content + library manifest endpoints.

Lets a peer node:
  - pull a single .enki document by content_sha256
  - pull a library's signed manifest
  - bulk-pull all .enki files in a library (paged)

This is the data layer of the multi-hub design. Hub coordinates
discovery (via /fleet/content/...) but the actual bytes flow
node-to-node via these endpoints.

Access control:
  - GET /p2p/content/{sha} → public for content in a library where
    is_published_to_network=TRUE. Other content returns 403.
  - GET /libraries/{id}/manifest → public iff is_public=TRUE.
  - GET /libraries/{id}/bootstrap → returns the manifest + the first
    page of doc hashes. Subscribers use this to start mirroring.
"""

from __future__ import annotations

import logging
from pathlib import Path
import os
from typing import Optional

from fastapi import APIRouter, HTTPException, Response, Query
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(tags=["p2p"])
log = logging.getLogger("enki.routes.p2p")


def _content_servable(content_sha256: str) -> Optional[str]:
    """Return the on-disk .enki path IFF the content is in at least one
    library marked is_published_to_network=TRUE. Otherwise None.

    Hex form ('aabbcc...') vs prefixed form ('sha256:aabbcc...') are
    both accepted; the column stores the hex form.
    """
    hex_only = content_sha256.split(":", 1)[-1].lower()
    if len(hex_only) != 64 or not all(c in "0123456789abcdef" for c in hex_only):
        return None

    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT d.enki_path
                    FROM documents d
                    WHERE d.content_sha256 = %s
                      AND EXISTS (
                        SELECT 1
                        FROM library_members lm
                        JOIN libraries l USING (library_id)
                        WHERE lm.member_kind = 'document'
                          AND lm.member_id = d.document_id
                          AND l.is_published_to_network = TRUE
                      )
                    LIMIT 1
                    """,
                    (hex_only,),
                )
                row = cur.fetchone()
                return row[0] if row else None
    except Exception as e:
        log.warning("p2p servable check failed for %s: %s", hex_only, e)
        return None


@router.head("/p2p/content/{content_sha256}")
async def head_p2p_content(content_sha256: str):
    """Cheap probe — does this node have content X?"""
    path = _content_servable(content_sha256)
    if not path or not Path(path).exists():
        raise HTTPException(status_code=404)
    return Response(status_code=200)


@router.get("/p2p/content/{content_sha256}")
async def get_p2p_content(content_sha256: str):
    """Serve a .enki file by content hash. Receiver verifies on receipt.

    Returns 404 if we don't have it, 403 if it's private (not in a
    network-published library).
    """
    hex_only = content_sha256.split(":", 1)[-1].lower()
    path = _content_servable(content_sha256)
    if not path:
        # Distinguish 'don't have it' vs 'private'.
        try:
            from storage.postgres.db import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 1 FROM documents WHERE content_sha256 = %s LIMIT 1",
                        (hex_only,),
                    )
                    if cur.fetchone():
                        raise HTTPException(status_code=403,
                                            detail="content not published to network")
        except HTTPException:
            raise
        except Exception:
            pass
        raise HTTPException(status_code=404, detail="not found")

    p = Path(path)
    if not p.exists():
        log.warning("p2p: documents row exists but enki_path missing on disk: %s", path)
        raise HTTPException(status_code=404, detail="content gone")

    return FileResponse(
        str(p),
        media_type="application/enki+cbor",
        filename=f"{hex_only}.enki",
        headers={
            "X-ENKI-Content-Hash": f"sha256:{hex_only}",
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


@router.get("/libraries/{library_id}/manifest.enki")
async def get_library_manifest(library_id: int,
                                v: Optional[int] = None,
                                since: Optional[str] = None):
    """Build + serve a signed .enki manifest of all documents in a
    library that are eligible for network sharing.

    Subscribers fetch this once on subscribe, then pull individual
    docs by content_sha256.

    Phase 5 (library federation plan) extends this to a v2 bundle
    manifest carrying documents + entities + extractions + relationships
    + notes + federation_id. Pass ?v=2 explicitly to opt in; default
    stays v1 for backward compat with existing subscribers in the
    field. Once Phase 5 subscribers are deployed, the default flips to v2.

    Phase 7 — delta sync. Pass ?since=<ISO-8601 timestamp> to filter
    every section to items added/published/produced after that time.
    Subscribers send their last_synced_at on subsequent ticks so the
    upstream returns only deltas, not the whole bundle. Empty result
    sections are still emitted (typed lists) so subscribers can confirm
    the upstream understood the request.
    """
    try:
        from storage.postgres.db import get_connection
        from core.enki.format import build_manifest
        from core.enki.ingest import get_node_signing_key
    except Exception as e:
        return JSONResponse(status_code=503,
                            content={"error": f"unavailable: {e}"})

    manifest_version = 2 if v == 2 else 1
    canonical_name = None
    docs: list = []
    doc_ids: list = []
    federation_id = None
    producer_node_id = (os.environ.get("ENKI_NODE_ID") or "").strip() or None
    entities: list = []
    extractions: list = []
    relationships: list = []
    co_producers: set = set()

    # Phase 7 delta cutoff. Parse once and reuse across section queries.
    since_dt = None
    if since:
        try:
            from datetime import datetime as _dt
            # Accept "Z" suffix or any ISO-8601 with offset
            since_clean = since.replace("Z", "+00:00")
            since_dt = _dt.fromisoformat(since_clean)
        except ValueError:
            return JSONResponse(status_code=400,
                                content={"error": f"invalid since param: {since!r} "
                                                  "— must be ISO-8601 timestamp"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT canonical_name, is_public, federation_id
                         FROM libraries WHERE library_id = %s""",
                    (library_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="library not found")
                canonical_name, is_public, federation_id = row
                if not is_public:
                    raise HTTPException(status_code=403,
                                        detail="library not public")

                # Mint federation_id on first v2 publish if missing — set
                # once, immutable thereafter. (UUIDv4 for now; UUIDv7 is
                # a future-compat tweak per the plan doc.)
                if manifest_version >= 2 and federation_id is None:
                    import uuid as _uuid
                    federation_id = str(_uuid.uuid4())
                    cur.execute(
                        """UPDATE libraries
                              SET federation_id = %s::uuid,
                                  original_producer_node_id = COALESCE(original_producer_node_id, %s),
                                  first_published_at        = COALESCE(first_published_at, NOW())
                            WHERE library_id = %s""",
                        (federation_id, producer_node_id, library_id),
                    )
                    conn.commit()

                # First: ALL doc IDs in the library (needed for v2 entity/
                # relationship scoping regardless of delta filter)
                cur.execute(
                    """
                    SELECT lm.member_id
                      FROM library_members lm
                     WHERE lm.library_id = %s
                       AND lm.member_kind = 'document'
                    """,
                    (library_id,),
                )
                all_doc_ids = [r[0] for r in cur.fetchall()]

                # Then: documents to actually emit in the manifest. Delta-
                # filter on library_members.added_at so subscribers
                # re-discover docs added since their last sync.
                if since_dt is not None:
                    cur.execute(
                        """
                        SELECT d.document_id, d.content_sha256, d.title, d.source_url,
                               COALESCE(LENGTH(d.on_disk_path), 0) AS size_hint
                        FROM library_members lm
                        JOIN documents d ON d.document_id = lm.member_id
                        WHERE lm.library_id = %s
                          AND lm.member_kind = 'document'
                          AND d.content_sha256 IS NOT NULL
                          AND COALESCE(lm.added_at, d.created_at) > %s
                        ORDER BY d.document_id
                        """,
                        (library_id, since_dt),
                    )
                else:
                    cur.execute(
                        """
                        SELECT d.document_id, d.content_sha256, d.title, d.source_url,
                               COALESCE(LENGTH(d.on_disk_path), 0) AS size_hint
                        FROM library_members lm
                        JOIN documents d ON d.document_id = lm.member_id
                        WHERE lm.library_id = %s
                          AND lm.member_kind = 'document'
                          AND d.content_sha256 IS NOT NULL
                        ORDER BY d.document_id
                        """,
                        (library_id,),
                    )
                rows_d = cur.fetchall()
                for r in rows_d:
                    doc_ids.append(r[0])
                    docs.append({
                        "content_hash": f"sha256:{r[1]}",
                        "source_title": r[2] or "",
                        "source_url":   r[3] or "",
                        "size_bytes":   int(r[4] or 0),
                    })

                if manifest_version >= 2 and all_doc_ids:
                    all_doc_shas: list = []
                    if all_doc_ids:
                        cur.execute(
                            "SELECT content_sha256 FROM documents WHERE document_id = ANY(%s) AND content_sha256 IS NOT NULL",
                            (all_doc_ids,),
                        )
                        all_doc_shas = [r[0] for r in cur.fetchall()]

                    # Entities — every entity with a mention in any library
                    # doc. Phase 7 delta: filter by e.created_at > since so
                    # subscribers only see NEW entities since last sync.
                    # Exclude reclassified rows so we don't export ghost
                    # entities to peers (they belong on places/topics/etc.
                    # substrate, exported via separate sync paths).
                    if since_dt is not None:
                        cur.execute(
                            """
                            SELECT e.canonical_entity_key, e.canonical_name,
                                   e.canonical_type, e.base60_address,
                                   COALESCE(e.signal_count, 0) AS signal_count,
                                   COALESCE(e.gdu_canonical, FALSE) AS gdu_canonical,
                                   ARRAY_AGG(DISTINCT d.content_sha256) AS evidence_doc_shas
                              FROM entities e
                              JOIN entity_mentions em ON em.entity_id = e.entity_id
                              JOIN documents d ON d.document_id = em.signal_rid
                             WHERE em.signal_rid = ANY(%s)
                               AND e.canonical_entity_key IS NOT NULL
                               AND e.created_at > %s
                               AND NOT (COALESCE(e.envelope, '{}'::jsonb) ? 'reclassification')
                             GROUP BY e.entity_id, e.canonical_entity_key,
                                      e.canonical_name, e.canonical_type,
                                      e.base60_address, e.signal_count,
                                      e.gdu_canonical
                            """,
                            (all_doc_ids, since_dt),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT e.canonical_entity_key, e.canonical_name,
                                   e.canonical_type, e.base60_address,
                                   COALESCE(e.signal_count, 0) AS signal_count,
                                   COALESCE(e.gdu_canonical, FALSE) AS gdu_canonical,
                                   ARRAY_AGG(DISTINCT d.content_sha256) AS evidence_doc_shas
                              FROM entities e
                              JOIN entity_mentions em ON em.entity_id = e.entity_id
                              JOIN documents d ON d.document_id = em.signal_rid
                             WHERE em.signal_rid = ANY(%s)
                               AND e.canonical_entity_key IS NOT NULL
                               AND NOT (COALESCE(e.envelope, '{}'::jsonb) ? 'reclassification')
                             GROUP BY e.entity_id, e.canonical_entity_key,
                                      e.canonical_name, e.canonical_type,
                                      e.base60_address, e.signal_count,
                                      e.gdu_canonical
                            """,
                            (all_doc_ids,),
                        )
                    for r in cur.fetchall():
                        entities.append({
                            "canonical_entity_key": r[0],
                            "canonical_name":       r[1],
                            "canonical_type":       r[2],
                            "base60_address":       r[3],
                            "signal_count":         int(r[4]),
                            "gdu_canonical":        bool(r[5]),
                            "evidence_doc_shas":    sorted(r[6] or []),
                        })

                    # Extractions — every (content_sha256, profile, producer)
                    # tuple published for library docs. Delta by published_at.
                    if since_dt is not None:
                        cur.execute(
                            """
                            SELECT content_sha256, profile_fingerprint,
                                   producer_node_id, result_envelope_sha,
                                   result_row_counts
                              FROM extraction_publications
                             WHERE content_sha256 = ANY(%s)
                               AND published_at > %s
                            """,
                            (all_doc_shas, since_dt),
                        )
                    else:
                        cur.execute(
                            """
                            SELECT content_sha256, profile_fingerprint,
                                   producer_node_id, result_envelope_sha,
                                   result_row_counts
                              FROM extraction_publications
                             WHERE content_sha256 = ANY(%s)
                            """,
                            (all_doc_shas,),
                        )
                    for r in cur.fetchall():
                        extractions.append({
                            "content_sha256":      r[0],
                            "profile_fingerprint": r[1],
                            "producer_node_id":    r[2],
                            "envelope_sha256":     r[3],
                            "row_counts":          r[4],
                        })
                        if r[2]:
                            co_producers.add(r[2])

                    # Relationships — entity_relationships where BOTH ends
                    # are in this library's entity set. Delta by er.created_at.
                    if since_dt is not None:
                        rel_filter = "AND er.created_at > %s"
                        rel_params = (all_doc_ids, all_doc_ids, since_dt)
                    else:
                        rel_filter = ""
                        rel_params = (all_doc_ids, all_doc_ids)
                    cur.execute(
                        f"""
                        SELECT es.canonical_entity_key AS source_ckey,
                               et.canonical_entity_key AS target_ckey,
                               er.relationship,
                               er.confidence,
                               COALESCE(er.evidence, ''),
                               er.source
                          FROM entity_relationships er
                          JOIN entities es ON es.entity_id = er.source_entity_id
                          JOIN entities et ON et.entity_id = er.target_entity_id
                         WHERE EXISTS (
                                 SELECT 1 FROM entity_mentions em
                                  WHERE em.entity_id = er.source_entity_id
                                    AND em.signal_rid = ANY(%s))
                           AND EXISTS (
                                 SELECT 1 FROM entity_mentions em
                                  WHERE em.entity_id = er.target_entity_id
                                    AND em.signal_rid = ANY(%s))
                           AND es.canonical_entity_key IS NOT NULL
                           AND et.canonical_entity_key IS NOT NULL
                           {rel_filter}
                        """,
                        rel_params,
                    )
                    for r in cur.fetchall():
                        relationships.append({
                            "source_ckey":  r[0],
                            "target_ckey":  r[1],
                            "relationship": r[2],
                            "confidence":   float(r[3]) if r[3] is not None else None,
                            "evidence":     (r[4] or "")[:1000],
                            "source":       r[5],
                        })
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"error": str(e)[:200]})

    # Empty result: short-circuit ONLY when this is a full snapshot
    # (since_dt is None). In delta mode, an empty docs[] is the common
    # case — analysts add notes on existing docs without adding docs —
    # so continue building the manifest with the other sections.
    if not docs and since_dt is None:
        return JSONResponse(
            status_code=200,
            content={
                "library_id": library_id,
                "canonical_name": canonical_name,
                "document_count": 0,
                "message": "library has no documents with content_sha256 yet",
            },
        )

    # Try to load the node signing key. In production (after install
    # ceremony) this lives at /var/lib/enki/node_key.bin and signs every
    # manifest so subscribers can verify upstream identity. In dev
    # environments where the key hasn't been provisioned (no install
    # ceremony has run), fall back to an unsigned manifest by generating
    # an ephemeral keypair just for this request. Subscribers can still
    # verify content via the inner content_sha256 of each document —
    # they just can't verify which node produced the manifest.
    try:
        privkey = get_node_signing_key()
    except (PermissionError, OSError) as e:
        # Key infrastructure not set up — generate ephemeral for this
        # response only. Log so this is visible.
        import logging
        from core.enki.format import generate_keypair
        logging.getLogger("enki.routes.p2p").warning(
            "node signing key unavailable (%s) — manifest signed with "
            "ephemeral keypair. Production installs run the install "
            "ceremony to provision /var/lib/enki/node_key.bin",
            e,
        )
        privkey = generate_keypair()

    # Phase 6: embed local library_notes in the v2 manifest so subscribers
    # mirror analyst annotations along with everything else.
    # Phase 7: filter by since_dt for delta sync.
    notes_payload: list = []
    if manifest_version >= 2:
        try:
            from core.federation.notes import fetch_notes_for_manifest
            notes_payload = fetch_notes_for_manifest(library_id, since=since_dt)
        except Exception as e:
            import logging
            logging.getLogger("enki.routes.p2p").warning(
                "v2 manifest notes fetch failed for library %s: %s",
                library_id, e,
            )

    manifest = build_manifest(
        library_id=library_id,
        library_canonical_name=canonical_name,
        manifest_version=manifest_version,
        documents=docs,
        signer_privkey=privkey,
        # v2 sections (ignored when manifest_version == 1)
        entities=entities,
        extractions=extractions,
        relationships=relationships,
        notes=notes_payload,
        federation_id=federation_id,
        producer_node_id=producer_node_id,
        co_producers=list(co_producers),
    )

    return Response(
        content=manifest.to_bytes(),
        media_type="application/enki+cbor",
        headers={
            "X-ENKI-Manifest-Hash":      manifest.content_hash,
            "X-ENKI-Library-Id":         str(library_id),
            "X-ENKI-Manifest-Version":   str(manifest_version),
            "X-ENKI-Document-Count":     str(len(docs)),
            "X-ENKI-Entity-Count":       str(len(entities)),
            "X-ENKI-Extraction-Count":   str(len(extractions)),
            "X-ENKI-Relationship-Count": str(len(relationships)),
            "X-ENKI-Note-Count":         str(len(notes_payload)),
            "X-ENKI-Federation-Id":      federation_id or "",
        },
    )


@router.get("/p2p/extraction/{content_sha256}/{profile_fingerprint}/{producer_node_id}")
async def serve_extraction_envelope(
    content_sha256: str,
    profile_fingerprint: str,
    producer_node_id: str,
):
    """Producer node serves the actual extraction envelope bytes (CBOR,
    deterministic, content-addressed by envelope_sha256). Subscribers
    pull this after locating the producer via /fleet/extractions/find.

    Path components:
      content_sha256       — 64 hex chars (no prefix), the underlying file
      profile_fingerprint  — extraction profile id (e.g. legacy_v1 or sha256)
      producer_node_id     — which node produced this envelope

    Receiver verifies the envelope by:
      1. Parsing the CBOR
      2. Computing sha256 over the canonical_json of the inner content
      3. Comparing to the envelope_sha they got from /fleet/extractions/find
    """
    from core.federation.extraction_publisher import get_envelope_bytes

    # Basic input sanity
    if len(content_sha256) != 64 or not all(c in "0123456789abcdef" for c in content_sha256):
        raise HTTPException(400, "content_sha256 must be 64 hex chars")
    if not profile_fingerprint or len(profile_fingerprint) > 128:
        raise HTTPException(400, "profile_fingerprint required, ≤128 chars")
    if not producer_node_id or len(producer_node_id) > 128:
        raise HTTPException(400, "producer_node_id required, ≤128 chars")

    body = get_envelope_bytes(content_sha256, profile_fingerprint, producer_node_id)
    if body is None:
        raise HTTPException(404, "extraction envelope not found on this node")

    return Response(
        content=body,
        media_type="application/enki+cbor",
        headers={
            "X-ENKI-Envelope-Type": "extraction",
            "X-ENKI-Content-Sha256": content_sha256,
            "X-ENKI-Profile-Fingerprint": profile_fingerprint,
            "X-ENKI-Producer-Node-Id": producer_node_id,
            # Envelopes are content-addressed → cache forever. The
            # consumer verifies the inner sha matches what was published.
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


@router.get("/libraries/{library_id}/bootstrap")
async def library_bootstrap(library_id: int,
                            limit: int = Query(500, ge=1, le=5000)):
    """JSON view of a library's content list for subscribers.

    Returns the canonical_name, document_count, and a paginated list
    of content_hashes the subscriber should pull. Subscribers iterate
    pages until exhausted.
    """
    try:
        from storage.postgres.db import get_connection
    except Exception as e:
        return JSONResponse(status_code=503,
                            content={"error": f"unavailable: {e}"})

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT canonical_name, is_public,
                              is_published_to_network
                         FROM libraries WHERE library_id = %s""",
                    (library_id,),
                )
                row = cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404)
                canonical_name, is_public, is_published = row
                if not is_public:
                    raise HTTPException(status_code=403,
                                        detail="library not public")

                cur.execute(
                    """SELECT count(*) FROM library_members lm
                        JOIN documents d ON d.document_id = lm.member_id
                       WHERE lm.library_id = %s
                         AND lm.member_kind = 'document'
                         AND d.content_sha256 IS NOT NULL""",
                    (library_id,),
                )
                total = int(cur.fetchone()[0] or 0)

                cur.execute(
                    """SELECT d.content_sha256, d.title, d.source_url
                         FROM library_members lm
                         JOIN documents d ON d.document_id = lm.member_id
                        WHERE lm.library_id = %s
                          AND lm.member_kind = 'document'
                          AND d.content_sha256 IS NOT NULL
                        ORDER BY d.document_id
                        LIMIT %s""",
                    (library_id, limit),
                )
                items = [
                    {
                        "content_sha256": r[0],
                        "title": r[1] or "",
                        "source_url": r[2] or "",
                    }
                    for r in cur.fetchall()
                ]
    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(status_code=500,
                            content={"error": str(e)[:200]})

    return {
        "library_id": library_id,
        "canonical_name": canonical_name,
        "is_published_to_network": is_published,
        "total_documents": total,
        "page_size": limit,
        "items": items,
    }
