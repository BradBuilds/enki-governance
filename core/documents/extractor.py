"""
Mention extractor — runs an LLM (or rule fallback) over paragraph text and
emits entity_mention rows.

Two entry points:

  extract_from_paragraph(paragraph_id, text, llm=...)
    → extracts mentions for one paragraph, persists rows

  extract_from_document(document_id, llm=..., dataset_id=...)
    → walks every paragraph in a document, calls extract_from_paragraph

The LLM is asked to return a JSON list of objects:
  [{"surface_form": "Will Marsh", "char_start": 412, "char_end": 424,
    "suggested_type": "person", "confidence": 0.92}, ...]

Each emitted mention is persisted via core.documents.mentions.persist_mention.
Resolution to canonical entities happens AFTER persistence — the worker
calls core.gdu.entity_resolution.resolve_entity for each mention, honoring
the per-dataset collapse policy. If the dataset is `review_required`, the
resolver creates an LDU entity + collapse_candidate; if not, it either
binds the mention to an existing canonical entity or creates a new one.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

from core.documents.mentions import persist_mention, normalize_surface
from storage.postgres.db import get_connection

log = logging.getLogger("enki.documents.extractor")


# ─── Prompt ──────────────────────────────────────────────────────────────

_MENTION_PROMPT_TEMPLATE = """\
You are a meticulous entity-mention extractor for an intelligence analysis system.

Read the following paragraph and extract every reference to a proper-noun entity
(person, organization, place, vessel, aircraft, facility, vehicle, product).
For each mention, return its surface form exactly as written, its character
offsets within the paragraph (0-indexed, half-open), and a type guess.

CRITICAL RULES:
- Be conservative. If you are not sure something refers to a real-world named entity,
  do NOT extract it. Pronouns ("he", "they") are NOT mentions unless you know who they refer to.
- Do not invent characters. char_start/char_end must point to the EXACT span as it
  appears in the text (verify the substring matches before emitting).
- Skip boilerplate (court header text, page numbers, "EXHIBIT NO.", "CASE NO.", etc.).
- If a name appears multiple times, emit a mention for EACH occurrence with its own offsets.
- Use type "person", "organization", "place", "vessel", "aircraft", "vehicle",
  "facility", or "product". If unsure, use "unknown".

Return ONLY a JSON object of the shape:
  {"mentions": [
     {"surface_form": "...", "char_start": N, "char_end": N,
      "suggested_type": "...", "confidence": 0.0..1.0}
  ]}

Paragraph text (between <text> tags):
<text>
{text}
</text>
"""

_MAX_PARAGRAPH_CHARS = 4000


# ─── LLM path ────────────────────────────────────────────────────────────

def _llm_extract(text: str, llm: Any) -> list[dict]:
    """Call the LLM and return the parsed mention list. Empty on failure."""
    if not text or not llm:
        return []

    truncated = text[:_MAX_PARAGRAPH_CHARS]
    # NOTE: do NOT use str.format() — the prompt contains literal JSON braces
    # ({"mentions": [...]}) that .format() would parse as placeholders and
    # raise KeyError. Replace just the {text} marker.
    prompt = _MENTION_PROMPT_TEMPLATE.replace("{text}", truncated)

    try:
        from core.ai.provider import LLMMessage
        messages = [
            LLMMessage(role="system",
                       content="You return only valid JSON. No commentary, no markdown."),
            LLMMessage(role="user", content=prompt),
        ]
        response = llm.complete(messages, max_tokens=1000, temperature=0.0)
    except Exception as e:
        log.debug("LLM call failed: %s", e)
        return []

    if response is None or getattr(response, "error", None):
        return []

    raw = (response.content or "").strip()
    # Models often wrap JSON in ```json ... ``` fences.
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
    if raw.endswith("```"):
        raw = raw[:-3]
    if raw.startswith("json"):
        raw = raw[4:].lstrip()

    try:
        parsed = json.loads(raw)
    except Exception:
        # Models sometimes emit prefix text. Salvage the first {...} block.
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            parsed = json.loads(raw[start:end])
        except Exception:
            return []

    if not isinstance(parsed, dict):
        return []
    mentions = parsed.get("mentions") or []
    if not isinstance(mentions, list):
        return []

    # Defensive validation: verify char_start/char_end actually point to the
    # surface form. The LLM gets this wrong sometimes — better to drop than
    # store a bogus span.
    out: list[dict] = []
    for m in mentions:
        if not isinstance(m, dict):
            continue
        surface = (m.get("surface_form") or "").strip()
        if not surface or len(surface) < 2:
            continue
        cs = m.get("char_start")
        ce = m.get("char_end")
        if isinstance(cs, int) and isinstance(ce, int) and 0 <= cs < ce <= len(truncated):
            actual = truncated[cs:ce]
            if actual.lower() != surface.lower():
                # Try to find the surface form anywhere in the text
                idx = truncated.find(surface)
                if idx < 0:
                    log.debug("LLM mention dropped (span mismatch + not found): %r", surface)
                    continue
                cs, ce = idx, idx + len(surface)
        else:
            # Coords missing or invalid — try to recover by searching
            idx = truncated.find(surface)
            if idx < 0:
                log.debug("LLM mention dropped (no coords + not found): %r", surface)
                continue
            cs, ce = idx, idx + len(surface)

        out.append({
            "surface_form": surface,
            "char_start": cs,
            "char_end": ce,
            "suggested_type": (m.get("suggested_type") or "unknown").lower(),
            "confidence": float(m.get("confidence") or 0.6),
        })
    return out


# ─── Rule fallback (for testing without an LLM) ─────────────────────────
# Conservative capitalized-multi-word regex. NOT a substitute for the LLM —
# misses single-word names, person-vs-org confusion, etc. But useful for
# unit tests + smoke tests + paragraphs the LLM declined.

_RULE_NAME_RE = re.compile(
    r"\b[A-Z][a-z]+(?:[\s]+(?:[A-Z][a-z]+|[A-Z]\.?))+\b"
)


def _rule_extract(text: str) -> list[dict]:
    out = []
    seen_spans: set[tuple[int, int]] = set()
    for m in _RULE_NAME_RE.finditer(text or ""):
        span = (m.start(), m.end())
        if span in seen_spans:
            continue
        seen_spans.add(span)
        surface = m.group(0)
        if len(surface) < 4:
            continue
        out.append({
            "surface_form": surface,
            "char_start": m.start(),
            "char_end": m.end(),
            "suggested_type": "unknown",
            "confidence": 0.45,
        })
    return out


# ─── Persistence orchestration ───────────────────────────────────────────

def extract_from_paragraph(
    *,
    tenant_id: str,
    document_id: int,
    page_id: int,
    paragraph_id: int,
    text: str,
    llm: Optional[Any] = None,
    rule_fallback: bool = True,
    extraction_engines: Optional[dict] = None,
) -> list[int]:
    """Extract + persist mentions for one paragraph. Returns mention_ids."""
    if not text or len(text.strip()) < 4:
        return []

    raw_mentions: list[dict] = []
    extraction_method = "none"

    if llm is not None:
        raw_mentions = _llm_extract(text, llm)
        extraction_method = "llm_only"

    if not raw_mentions and rule_fallback:
        raw_mentions = _rule_extract(text)
        extraction_method = "rule_only" if raw_mentions else "none"

    mention_ids: list[int] = []
    for m in raw_mentions:
        mid = persist_mention(
            tenant_id=tenant_id,
            document_id=document_id,
            page_id=page_id,
            paragraph_id=paragraph_id,
            char_start=m["char_start"],
            char_end=m["char_end"],
            surface_form=m["surface_form"],
            suggested_type=m["suggested_type"],
            confidence=m["confidence"],
            resolution_status="unresolved",
            extraction_method=extraction_method,
            extraction_engines=extraction_engines,
        )
        mention_ids.append(mid)
    return mention_ids


def extract_from_document(
    *,
    tenant_id: str,
    document_id: int,
    llm: Optional[Any] = None,
    rule_fallback: bool = True,
    paragraph_limit: Optional[int] = None,
    skip_boilerplate: bool = True,
) -> dict:
    """Walk all paragraphs in a document and extract mentions for each.
    Skips paragraphs already flagged is_boilerplate (boilerplate clustering
    runs before extraction in the standard pipeline).

    Returns: {paragraphs_processed, mentions_extracted, document_id}.

    Federation race-stop (Phase 4 of library federation plan): if the
    extraction_subscriber has already pulled this doc's extraction from a
    federation peer, skip the local LLM entirely. Same if a claim is in
    flight via the work-claim protocol.
    """
    # ─── Federation race-stop (local state) ───────────────────────────
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT extraction_state, content_sha256 FROM documents WHERE document_id = %s",
                (document_id,),
            )
            row = cur.fetchone()
            existing_state = row[0] if row else None
            content_sha256 = row[1] if row else None

    if existing_state in ('pulled_from_network', 'local_running', 'local_complete',
                          'claimed_by_peer'):
        log.info(
            "Document %d: extraction skipped (state=%s — federation or another worker has it)",
            document_id, existing_state,
        )
        return {
            "document_id": document_id,
            "paragraphs_processed": 0,
            "mentions_extracted": 0,
            "skipped": True,
            "skip_reason": existing_state,
        }

    # ─── Federation work-claim (hub check + claim) ────────────────────
    # Before launching a 30+ min local LLM pass, ask the hub: is this
    # work already done OR being done by a peer? If so, skip + let the
    # extraction_subscriber pull the peer's envelope when ready.
    #
    # Fail-open on all hub errors: a flaky hub must NEVER prevent local
    # extraction. The cost of "redundant extraction" is < the cost of
    # "extraction blocked because hub is down". Claims default to 1h TTL
    # so a crashed extractor doesn't hold the slot forever.
    hub_url = os.environ.get("ENKI_HUB_URL")
    claim_id: Optional[int] = None
    claim_profile_id: Optional[str] = None
    claim_content_sha256: Optional[str] = content_sha256
    if hub_url and content_sha256:
        try:
            from core.extraction.profile_registry import derive_profile_from_llm
            from storage.node.identity import load_node_id
            import requests as _rq
            claim_profile_id = derive_profile_from_llm(
                llm,
                prompt_template="document_extraction_v1",
                prompt_version="v1",
            )
            node_id = load_node_id() or os.environ.get("ENKI_NODE_ID", "unknown-node")

            check_r = _rq.get(
                f"{hub_url}/fleet/extractions/check",
                params={"content": content_sha256, "profile": claim_profile_id},
                timeout=5,
            )
            if check_r.ok:
                advice = (check_r.json() or {}).get("advice")
                if advice in ("skip", "wait"):
                    log.info(
                        "Document %d: federation work-claim advice=%s — skipping local",
                        document_id, advice,
                    )
                    try:
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE documents SET extraction_state = 'claimed_by_peer' WHERE document_id = %s",
                                    (document_id,),
                                )
                            conn.commit()
                    except Exception:
                        pass
                    return {
                        "document_id": document_id, "paragraphs_processed": 0,
                        "mentions_extracted": 0, "skipped": True,
                        "skip_reason": f"federation:{advice}",
                    }

                claim_r = _rq.post(
                    f"{hub_url}/fleet/extractions/claim",
                    params={
                        "content": content_sha256, "profile": claim_profile_id,
                        "claimant_node_id": node_id, "ttl_seconds": 3600,
                    },
                    timeout=5,
                )
                if claim_r.status_code == 409:
                    log.info("Document %d: claim lost to peer — skipping local",
                             document_id)
                    try:
                        with get_connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute(
                                    "UPDATE documents SET extraction_state = 'claimed_by_peer' WHERE document_id = %s",
                                    (document_id,),
                                )
                            conn.commit()
                    except Exception:
                        pass
                    return {
                        "document_id": document_id, "paragraphs_processed": 0,
                        "mentions_extracted": 0, "skipped": True,
                        "skip_reason": "federation:claim_lost",
                    }
                if claim_r.ok:
                    claim_id = (claim_r.json() or {}).get("claim_id")
                    log.info("Document %d: acquired federation claim %s (profile=%s)",
                             document_id, claim_id, claim_profile_id)
        except Exception as e:
            log.debug("federation check/claim failed, proceeding local (%s)", e)
            claim_id = None  # no claim held; nothing to release

    # Mark this doc as locally extracting so a concurrent subscriber tick
    # doesn't double-process it. Best-effort; idempotent vs sub.
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE documents
                          SET extraction_state = 'local_running'
                        WHERE document_id = %s
                          AND (extraction_state IS NULL OR extraction_state = 'pending_local')""",
                    (document_id,),
                )
            conn.commit()
    except Exception:
        # extraction_state column may not exist yet on older deployments;
        # don't fail extraction over the state stamp
        pass

    where = ["document_id = %s"]
    params: list = [document_id]
    if skip_boilerplate:
        where.append("is_boilerplate = FALSE")
    sql = f"""
        SELECT paragraph_id, page_id, raw_text
        FROM document_paragraphs
        WHERE {' AND '.join(where)}
        ORDER BY page_id, order_index
    """
    if paragraph_limit:
        sql += f" LIMIT {int(paragraph_limit)}"

    paragraphs: list[tuple] = []
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            paragraphs = cur.fetchall()

    extraction_engines = {}
    if llm is not None:
        engine_name = getattr(llm, "model", None) or getattr(llm, "__class__", type(llm)).__name__
        extraction_engines = {"llm": str(engine_name)}

    total_mentions = 0
    extraction_failed = False
    try:
        for paragraph_id, page_id, text in paragraphs:
            ids = extract_from_paragraph(
                tenant_id=tenant_id, document_id=document_id,
                page_id=page_id, paragraph_id=paragraph_id, text=text,
                llm=llm, rule_fallback=rule_fallback,
                extraction_engines=extraction_engines,
            )
            total_mentions += len(ids)

        log.info(
            "Document %d: extracted %d mentions across %d paragraphs",
            document_id, total_mentions, len(paragraphs),
        )

        # Stamp local_complete so the federation subscriber's race-stop sees
        # a definitive end-state. Publisher (below) then ships the envelope
        # to the hub so peers can pull instead of re-running the LLM.
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE documents SET extraction_state = 'local_complete' WHERE document_id = %s",
                        (document_id,),
                    )
                conn.commit()
        except Exception:
            pass

        # Auto-publish to federation hub. Fully non-fatal — a publish failure
        # must never break local extraction. Skipped when ENKI_HUB_URL is
        # unset (local-only deploy, dev environments).
        if hub_url:
            try:
                from core.federation.extraction_publisher import publish_for_document
                r = publish_for_document(
                    document_id=document_id,
                    tenant_id=tenant_id,
                    hub_url=hub_url,
                )
                if r.get("published"):
                    log.info("Document %d: published %d extraction envelope(s) to %s",
                             document_id, r["published"], hub_url)
                elif r.get("reason"):
                    log.debug("Document %d: publish skipped (%s)", document_id, r["reason"])
            except Exception as e:
                log.warning("Document %d: auto-publish failed (non-fatal): %s",
                            document_id, e)
    except Exception:
        extraction_failed = True
        raise
    finally:
        # Release the federation claim (if held). outcome=completed on
        # success so peers know an envelope exists; abandoned on failure
        # so peers can try. Fail-open — release errors never propagate.
        if claim_id is not None and hub_url:
            outcome = "abandoned" if extraction_failed else "completed"
            try:
                import requests as _rq
                from storage.node.identity import load_node_id
                _node_id = load_node_id() or os.environ.get("ENKI_NODE_ID", "unknown-node")
                _rq.post(
                    f"{hub_url}/fleet/extractions/release",
                    params={
                        "claim_id": claim_id,
                        "claimant_node_id": _node_id,
                        "outcome": outcome,
                    },
                    timeout=5,
                )
                log.debug("Document %d: released federation claim %s (%s)",
                          document_id, claim_id, outcome)
            except Exception as e:
                log.debug("federation release failed (non-fatal): %s", e)

    return {
        "document_id": document_id,
        "paragraphs_processed": len(paragraphs),
        "mentions_extracted": total_mentions,
    }
