"""
extraction_subscriber.py — pulls federation extraction envelopes and applies
them locally, with no LLM re-run.

This is Phase 3 of the Library Federation plan
(the internal library federation plan). Counterpart to
core/federation/extraction_publisher.py (Phase 2).

Tick loop:
  1. SELECT documents where content_sha256 is set but no entity_mentions exist
  2. For each: GET /fleet/extractions/find?content={sha} on the hub
  3. Select a winning producer (G6 trust + G7 consensus)
  4. GET {producer.pull_base_url}{producer.pull_url} → CBOR bytes
  5. Apply gates G1..G5 → envelope or REJECT
  6. _apply_extraction_envelope() in a single transaction (G8, G9)
  7. Stamp documents.extraction_state = 'pulled_from_network'
  8. Audit every outcome to extraction_pull_audit (G10)

Ten verification gates:
  node-A  format string matches "enki-extraction-envelope-v2"
  G2  envelope.content_sha256 == the doc we asked for
  G3  sha256(canonical_json(envelope.content)) == envelope.envelope_sha256
  G4  envelope.envelope_sha256 == hub_claimed_sha (bait-and-switch)
  G5  envelope size <= MAX_ENVELOPE_BYTES; row counts within caps
  G6  producer.trust_score >= MIN_TRUST_SCORE (filtered at find time)
  G7  consensus.unique_envelopes == 1 OR pick majority_envelope_sha
  G8  per-row sanitation: length caps, numeric ranges, FK remap via
       entity_id_map (NEVER trust producer's local PKs)
  G9  atomic transaction; any row fails → ROLLBACK + quarantine state
  G10 every outcome (accepted/rejected/quarantined) logged to
       extraction_pull_audit with reason_code

Entity resolution (G8 core mechanic):
  For each entity in envelope.content.entities[]:
    SELECT entity_id FROM entities
     WHERE tenant_id=%s AND canonical_entity_key = %s
     ORDER BY signal_count DESC NULLS LAST, entity_id ASC LIMIT 1
  If found: entity_id_map[producer_entity_id] = local_id
  Else: INSERT new entities row with full GDU state from envelope,
        tag with pulled_from_node_id, use new id

Then every reference (mention.entity_id, statement.speaker_entity_id, etc.)
is REMAPPED via entity_id_map before insert. Producer's local PKs are
never written to local DB.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("enki.workers.extraction_subscriber")

# ── Configuration ─────────────────────────────────────────────────────────

HUB_URL = (os.environ.get("ENKI_HUB_URL") or "").rstrip("/")

_TICK_INTERVAL_S = int(os.environ.get("ENKI_EXTRACTION_SUBSCRIBER_TICK_S", "900"))
_IDLE_BACKOFF_S  = int(os.environ.get("ENKI_EXTRACTION_SUBSCRIBER_BACKOFF_S", "1800"))

# G5: envelope must be reasonably sized
MAX_ENVELOPE_BYTES   = int(os.environ.get("ENKI_MAX_ENVELOPE_BYTES", str(10 * 1024 * 1024)))    # 10 MB
MAX_ENTITIES_PER_ENV = int(os.environ.get("ENKI_MAX_ENTITIES_PER_ENV", "5000"))
MAX_ROWS_PER_KIND    = int(os.environ.get("ENKI_MAX_ROWS_PER_KIND",    "100000"))

# G6: producer trust threshold
MIN_TRUST_SCORE      = float(os.environ.get("ENKI_MIN_PRODUCER_TRUST", "0.5"))

# G0: Phase 10 envelope-signature enforcement.
#   'required' (production) — reject envelopes without a valid signature
#   'warn'      (default)    — accept unsigned but log a warning
#   'off'                    — never check (legacy / smoke-test escape hatch)
SIGNATURE_MODE = os.environ.get("ENKI_ENVELOPE_SIGNATURE_MODE", "warn").lower()

# In-memory pubkey cache so we don't refetch the hub on every envelope.
# Keyed by producer_node_id → (pubkey_bytes, fetched_at_unix).
_PUBKEY_CACHE: dict = {}
_PUBKEY_CACHE_TTL_S = 3600

# G8: per-row sanitation caps
MAX_SURFACE_FORM_LEN = 2000
MAX_PREDICATE_LEN    = 256
MAX_RAW_TEXT_LEN     = 10000
MAX_CANONICAL_NAME   = 500
MAX_CANONICAL_TYPE   = 128

# Per-tick rate limit so one bad doc can't starve the loop
MAX_DOCS_PER_TICK    = int(os.environ.get("ENKI_MAX_DOCS_PER_SUB_TICK", "50"))
HTTP_TIMEOUT_S       = 60

EXPECTED_FORMAT      = "enki-extraction-envelope-v2"


# ── Worker globals (match library_subscriber.py shape) ───────────────────

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "ticks": 0,
    "docs_processed": 0,
    "envelopes_applied": 0,
    "envelopes_rejected": 0,
    "envelopes_quarantined": 0,
    "rows_inserted": 0,
    "errors": 0,
    "last_error": None,
}


def get_status() -> dict:
    return dict(_STATUS)


def _enabled() -> bool:
    return bool(HUB_URL)


def start_worker(tenant_id: str = "default") -> dict:
    """Spawn the background loop. No-op if ENKI_HUB_URL isn't set."""
    global _WORKER_THREAD
    if not _enabled():
        return {"ok": False, "reason": "ENKI_HUB_URL not set"}
    if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
        return {"ok": True, "already_running": True}
    _STOP_EVENT.clear()
    _STATUS.update(running=True, phase="started", last_error=None)
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,), name="extraction-subscriber",
        daemon=True,
    )
    _WORKER_THREAD.start()
    return {"ok": True, "started": True}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    _STATUS.update(running=False, phase="stopped")
    return {"ok": True, "stopped": True}


def _loop(tenant_id: str) -> None:
    """Long-running worker. Calls run_tick() every 15 min; backs off to
    30 min when nothing was pulled."""
    while not _STOP_EVENT.is_set():
        try:
            _STATUS.update(phase="ticking")
            result = run_tick(tenant_id=tenant_id)
            _STATUS["ticks"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            interval = _TICK_INTERVAL_S if result.get("envelopes_applied", 0) > 0 \
                       else _IDLE_BACKOFF_S
        except Exception as e:
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            log.exception("extraction_subscriber tick failed")
            interval = _IDLE_BACKOFF_S

        # Sleep responsively
        _STATUS["phase"] = "sleeping"
        deadline = time.time() + interval
        while time.time() < deadline and not _STOP_EVENT.is_set():
            time.sleep(1)


# ── The work ──────────────────────────────────────────────────────────────


def run_tick(*, tenant_id: str = "default") -> dict:
    """Walk docs that have content_sha256 but no entity_mentions yet, try
    to pull each from the federation. Returns aggregate counters."""
    if not _enabled():
        return {"reason": "ENKI_HUB_URL not set"}

    from storage.postgres.db import get_connection

    totals = {
        "docs_processed":       0,
        "envelopes_applied":    0,
        "envelopes_rejected":   0,
        "envelopes_quarantined": 0,
        "rows_inserted":        0,
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT d.document_id, d.content_sha256
                  FROM documents d
                 WHERE d.tenant_id = %s
                   AND d.content_sha256 IS NOT NULL
                   AND (d.extraction_state IS NULL
                        OR d.extraction_state IN ('pending_local',
                                                  'claimed_by_peer'))
                   AND NOT EXISTS (
                       SELECT 1 FROM entity_mentions em
                        WHERE em.signal_rid = d.document_id
                   )
                 ORDER BY d.document_id ASC
                 LIMIT %s
                """,
                (tenant_id, MAX_DOCS_PER_TICK),
            )
            docs = cur.fetchall()

    for doc_id, content_sha in docs:
        totals["docs_processed"] += 1
        try:
            result = _pull_and_apply_one_doc(doc_id, content_sha, tenant_id=tenant_id)
        except Exception as e:
            log.warning("pull/apply failed for doc_id=%s sha=%s: %s",
                        doc_id, content_sha[:16], e)
            _STATUS["errors"] += 1
            _audit(content_sha256=content_sha, profile_fingerprint=None,
                   producer_node_id=None, decision="rejected",
                   reason_code="exception", reason_detail=str(e)[:300])
            continue

        if result.get("applied"):
            totals["envelopes_applied"] += 1
            totals["rows_inserted"] += result.get("rows", 0)
        elif result.get("quarantined"):
            totals["envelopes_quarantined"] += 1
        else:
            totals["envelopes_rejected"] += 1

    _STATUS["docs_processed"] += totals["docs_processed"]
    _STATUS["envelopes_applied"] += totals["envelopes_applied"]
    _STATUS["envelopes_rejected"] += totals["envelopes_rejected"]
    _STATUS["envelopes_quarantined"] += totals["envelopes_quarantined"]
    _STATUS["rows_inserted"] += totals["rows_inserted"]
    return totals


def _pull_and_apply_one_doc(document_id: int, content_sha256: str,
                             *, tenant_id: str) -> dict:
    """Full lifecycle for one doc: find producer → fetch envelope →
    verify (G1..G5) → apply (G8, G9) → audit (G10)."""
    started_at = time.time()

    # === Find ===
    producers, consensus = _find_producers(content_sha256)
    if not producers:
        _audit(content_sha256=content_sha256, profile_fingerprint=None,
               producer_node_id=None, decision="rejected",
               reason_code="no_producer")
        return {"applied": False, "reason": "no_producer"}

    # G7: consensus check
    winner = _pick_winner(producers, consensus)
    if winner is None:
        _audit(content_sha256=content_sha256, profile_fingerprint=None,
               producer_node_id=None, decision="rejected",
               reason_code="g7_no_consensus")
        return {"applied": False, "reason": "g7_no_consensus"}

    profile_fingerprint = winner["profile_fingerprint"]
    producer_node_id = winner["producer_node_id"]
    hub_claimed_sha = winner["envelope_sha"]

    # === Fetch ===
    # Phase 9: prefer direct producer pull when pull_base_url is known
    # and reachable; fall back to hub mirror when the producer is
    # behind a firewall (Cloudflare Access, NAT, etc.).
    pull_base = winner.get("pull_base_url")
    hub_pull_url = winner.get("hub_pull_url")  # path on hub if mirrored
    body = None
    fetch_err = None
    tried = []

    if pull_base:
        url = f"{pull_base.rstrip('/')}{winner['pull_url']}"
        tried.append("producer_direct")
        body, fetch_err = _http_get_bytes(url, MAX_ENVELOPE_BYTES)

    if body is None and hub_pull_url and HUB_URL:
        url = f"{HUB_URL}{hub_pull_url}"
        tried.append("hub_mirror")
        body, fetch_err = _http_get_bytes(url, MAX_ENVELOPE_BYTES)

    if body is None:
        # Nothing worked. Audit with the most informative reason.
        if not pull_base and not hub_pull_url:
            reason, detail = "no_pull_source", "producer not registered + bytes not mirrored on hub"
        else:
            reason, detail = "fetch_failed", f"tried={tried}; last_err={fetch_err}"
        _audit(content_sha256=content_sha256, profile_fingerprint=profile_fingerprint,
               producer_node_id=producer_node_id,
               hub_claimed_sha=hub_claimed_sha,
               decision="rejected", reason_code=reason,
               reason_detail=detail)
        return {"applied": False, "reason": reason}

    # === Gates G1..G5 ===
    envelope, gate_err = _verify_envelope(body, content_sha256, hub_claimed_sha)
    if gate_err:
        _audit(content_sha256=content_sha256, profile_fingerprint=profile_fingerprint,
               producer_node_id=producer_node_id,
               hub_claimed_sha=hub_claimed_sha,
               decision="rejected", reason_code=gate_err["code"],
               reason_detail=gate_err.get("detail"))
        return {"applied": False, "reason": gate_err["code"]}

    # === Apply (G8 + G9) ===
    apply_result = _apply_extraction_envelope(
        envelope=envelope,
        document_id=document_id,
        producer_node_id=producer_node_id,
        tenant_id=tenant_id,
    )

    # === G10 audit ===
    decision = apply_result["decision"]
    _audit(
        content_sha256=content_sha256,
        profile_fingerprint=profile_fingerprint,
        producer_node_id=producer_node_id,
        envelope_sha=envelope["envelope_sha256"],
        hub_claimed_sha=hub_claimed_sha,
        decision=decision,
        reason_code=apply_result.get("reason_code", "ok"),
        reason_detail=apply_result.get("reason_detail"),
        row_counts_applied=apply_result.get("row_counts"),
        duration_ms=int((time.time() - started_at) * 1000),
    )

    if decision == "accepted":
        return {"applied": True, "rows": apply_result.get("rows", 0)}
    if decision == "quarantined":
        return {"applied": False, "quarantined": True}
    return {"applied": False, "reason": apply_result.get("reason_code", "unknown")}


# ── Find + winner selection ──────────────────────────────────────────────


def _find_producers(content_sha256: str) -> tuple[list, dict]:
    """Hub query for producers of this content. Returns (producers, consensus).
    G6 filtering happens server-side via min_trust query param."""
    try:
        import httpx
        from core.federation.peer_identity import authorized_headers
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            r = client.get(
                f"{HUB_URL}/fleet/extractions/find",
                params={"content": content_sha256, "min_trust": MIN_TRUST_SCORE},
                headers=authorized_headers(),
            )
            if r.status_code != 200:
                return [], {}
            data = r.json()
            return data.get("producers", []) or [], data.get("consensus", {}) or {}
    except Exception as e:
        log.warning("find_producers failed for %s: %s", content_sha256[:16], e)
        return [], {}


def _pick_winner(producers: list, consensus: dict) -> Optional[dict]:
    """G7: prefer majority_envelope_sha when there's consensus.
    If unique_envelopes > 1 and no clear majority, REFUSE."""
    if not producers:
        return None
    if len(producers) == 1:
        return producers[0]

    majority_sha = consensus.get("majority_envelope_sha")
    unique = int(consensus.get("unique_envelopes") or 1)
    if unique == 1:
        # All producers agree; pick highest-trust
        return producers[0]

    if majority_sha:
        # Pick highest-trust producer in the majority bucket
        for p in producers:
            if p.get("envelope_sha") == majority_sha:
                return p

    # No clear winner → refuse (G7 minority outlier protection)
    return None


# ── Fetch + verify (G1..G5) ──────────────────────────────────────────────


def _http_get_bytes(url: str, max_bytes: int) -> tuple[Optional[bytes], Optional[str]]:
    try:
        import httpx
        from core.federation.peer_identity import authorized_headers
        with httpx.Client(timeout=HTTP_TIMEOUT_S) as client:
            r = client.get(url, headers=authorized_headers())
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}"
            body = r.content
            if len(body) > max_bytes:
                return None, f"oversize {len(body)} > {max_bytes}"
            return body, None
    except Exception as e:
        return None, str(e)[:300]


def _fetch_producer_pubkey(producer_node_id: str) -> Optional[bytes]:
    """Hub-mediated pubkey discovery for envelope signature verification.
    Cached per process for _PUBKEY_CACHE_TTL_S seconds so we don't pound
    the hub on every envelope. Returns raw 32-byte Ed25519 pubkey or None."""
    import base64, time
    if not producer_node_id:
        return None
    cached = _PUBKEY_CACHE.get(producer_node_id)
    if cached and (time.time() - cached[1]) < _PUBKEY_CACHE_TTL_S:
        return cached[0]
    try:
        import httpx
        from core.federation.peer_identity import authorized_headers
        with httpx.Client(timeout=10) as client:
            r = client.get(
                f"{HUB_URL}/fleet/peer/pubkey/{producer_node_id}",
                headers=authorized_headers(),
            )
            if r.status_code != 200:
                return None
            data = r.json()
            pubkey_b64 = data.get("pubkey_b64")
            if not pubkey_b64:
                return None
            pubkey_bytes = base64.b64decode(pubkey_b64)
            if len(pubkey_bytes) != 32:
                return None
            _PUBKEY_CACHE[producer_node_id] = (pubkey_bytes, time.time())
            return pubkey_bytes
    except Exception as e:
        log.warning("pubkey fetch failed for %s: %s", producer_node_id, e)
        return None


def _verify_envelope_signature(envelope: dict) -> tuple[bool, Optional[str]]:
    """G0: verify envelope.signature against producer's Ed25519 pubkey.
    Returns (ok, reason). reason describes the failure mode.

    SIGNATURE_MODE controls enforcement:
      - 'required' → reject unsigned + reject invalid → ok=False on any miss
      - 'warn'     → accept unsigned (with reason='warn_unsigned') but
                     still reject invalid signatures
      - 'off'      → never check
    """
    if SIGNATURE_MODE == "off":
        return True, None

    import base64
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    sig_b64 = envelope.get("signature")
    producer = envelope.get("producer_node_id") or ""
    envelope_sha = envelope.get("envelope_sha256") or ""

    if not sig_b64:
        if SIGNATURE_MODE == "required":
            return False, "g0_unsigned"
        log.warning("envelope from %s has no signature (mode=warn — accepting)",
                    producer)
        return True, "warn_unsigned"

    pubkey_bytes = _fetch_producer_pubkey(producer)
    embedded_pubkey_b64 = envelope.get("signer_pubkey_b64")
    used_embedded = False

    if not pubkey_bytes:
        # Hub doesn't know this producer. In strict mode we reject.
        # In warn mode we DON'T silently pass — we fall back to the
        # producer-embedded pubkey so transit tampering (a hostile
        # middlebox flipping bits) is still caught. The embedded
        # pubkey IS producer-self-attested, so this defends against
        # in-flight tampering but NOT against a hostile producer who
        # crafted both bytes + signature from scratch. Strict mode
        # closes that residual gap by requiring hub registration.
        if SIGNATURE_MODE == "required":
            return False, "g0_no_pubkey"
        if embedded_pubkey_b64:
            try:
                pubkey_bytes = base64.b64decode(embedded_pubkey_b64)
                if len(pubkey_bytes) == 32:
                    used_embedded = True
                else:
                    pubkey_bytes = None
            except Exception:
                pubkey_bytes = None
        if not pubkey_bytes:
            log.warning("can't fetch pubkey for %s and no embedded fallback "
                        "(mode=warn — accepting unsigned)", producer)
            return True, "warn_no_pubkey"

    try:
        sig = base64.b64decode(sig_b64)
        Ed25519PublicKey.from_public_bytes(pubkey_bytes).verify(
            sig, envelope_sha.encode("utf-8")
        )
        if used_embedded:
            log.warning("verified signature from %s via embedded pubkey "
                        "(hub has no registration) — register the peer "
                        "to close the residual self-attestation gap",
                        producer)
            return True, "warn_embedded_pubkey"
        return True, None
    except (InvalidSignature, ValueError) as e:
        # ALWAYS reject an invalid signature — even in warn mode. The
        # distinction between warn and required is "missing vs invalid":
        # missing is graceful, invalid is hostile.
        return False, f"g0_signature_invalid:{type(e).__name__}"


def _verify_envelope(body: bytes, expected_content_sha: str,
                      hub_claimed_sha: str) -> tuple[Optional[dict], Optional[dict]]:
    """Apply gates G0..G5. Returns (envelope, None) on pass, or (None, error_dict)."""
    # G5a: outer size already checked by _http_get_bytes
    try:
        import cbor2
        envelope = cbor2.loads(body)
    except Exception as e:
        return None, {"code": "g1_cbor_parse", "detail": str(e)[:200]}

    if not isinstance(envelope, dict):
        return None, {"code": "g1_not_dict"}

    # node-A: format check
    if envelope.get("format") != EXPECTED_FORMAT:
        return None, {"code": "g1_format_unknown",
                      "detail": f"got {envelope.get('format')}"}

    content = envelope.get("content")
    if not isinstance(content, dict):
        return None, {"code": "g1_no_content"}

    # G2: content_sha256 inside envelope must match requested doc
    if envelope.get("content_sha256") != expected_content_sha:
        return None, {"code": "g2_content_mismatch",
                      "detail": f"envelope {envelope.get('content_sha256','?')[:16]} != requested {expected_content_sha[:16]}"}
    if content.get("content_sha256") != expected_content_sha:
        return None, {"code": "g2_inner_mismatch"}

    # G3: recompute sha256 over canonical_json(content) — BYTE-IDENTICAL
    # recipe to publisher (json.dumps with sort_keys + minimal separators)
    canonical_json = json.dumps(content, sort_keys=True, separators=(",", ":"))
    recomputed = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    claimed = envelope.get("envelope_sha256")
    if recomputed != claimed:
        return None, {"code": "g3_sha_mismatch",
                      "detail": f"computed {recomputed[:16]} != claimed {claimed[:16] if claimed else '?'}"}

    # G4: hub-claimed sha must match what's actually in the bytes
    # (defends against producer bait-and-switch with the hub)
    if hub_claimed_sha and recomputed != hub_claimed_sha:
        return None, {"code": "g4_hub_mismatch",
                      "detail": f"envelope {recomputed[:16]} != hub claim {hub_claimed_sha[:16]}"}

    # G5: row count caps
    counts = envelope.get("row_counts") or {}
    if counts.get("entities", 0) > MAX_ENTITIES_PER_ENV:
        return None, {"code": "g5_entities_over_cap"}
    for k in ("mentions", "statements", "assessments"):
        if counts.get(k, 0) > MAX_ROWS_PER_KIND:
            return None, {"code": f"g5_{k}_over_cap"}

    # G0: Phase 10 envelope signature verification. Last gate before
    # apply because it requires a hub round-trip (or cache hit) and we
    # want to fail fast on the cheaper static checks first.
    sig_ok, sig_reason = _verify_envelope_signature(envelope)
    if not sig_ok:
        return None, {"code": sig_reason or "g0_signature_unknown"}

    return envelope, None


# ── Apply (G8 + G9) ──────────────────────────────────────────────────────


def _apply_extraction_envelope(*, envelope: dict, document_id: int,
                                producer_node_id: str, tenant_id: str) -> dict:
    """Atomic insert of all rows from a verified envelope. G8 sanitation
    and FK remapping inside; G9 transaction wraps the whole thing.

    Returns dict with: decision ('accepted'|'rejected'|'quarantined'),
    reason_code, reason_detail, row_counts, rows."""
    from storage.postgres.db import get_connection

    content = envelope["content"]
    envelope_sha = envelope["envelope_sha256"]
    profile = envelope.get("profile_fingerprint") or content.get("profile_fingerprint")

    entities_payload   = content.get("entities", []) or []
    mentions_payload   = content.get("mentions", []) or []
    statements_payload = content.get("statements", []) or []
    assessments_payload = content.get("assessments", []) or []

    rows_inserted = {"entities_new": 0, "entity_mentions": 0,
                     "document_mentions": 0, "statements": 0, "assessments": 0}

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # ─── G8: resolve / create entities via canonical_entity_key ───
                entity_id_map: dict[int, int] = {}
                for e in entities_payload:
                    ckey = e.get("canonical_entity_key")
                    name = (e.get("canonical_name") or "").strip()
                    etype = (e.get("canonical_type") or "").strip()
                    pid = e.get("producer_entity_id")

                    if not ckey or len(ckey) != 64 or not name or pid is None:
                        # Skip but DON'T fail — the envelope can have entries
                        # we can't use without poisoning the FK remap
                        continue
                    # G8: verify the ckey matches sha256(name|type) on our side
                    recomputed_ckey = _canonical_key(name, etype)
                    if recomputed_ckey != ckey:
                        return _abort("g8_ckey_mismatch",
                                      f"entity #{pid}: producer says {ckey[:16]}, "
                                      f"locally {recomputed_ckey[:16]}")

                    # G8 length caps
                    if len(name) > MAX_CANONICAL_NAME or len(etype) > MAX_CANONICAL_TYPE:
                        continue  # silently drop oversized entities

                    # Subtype lockdown — refuse to mirror peer rows whose
                    # canonical_type isn't an admittable entityvocab subtype.
                    # Migration 116's CHECK would reject the INSERT anyway.
                    # Skipped entities are absent from entity_id_map, so
                    # peer-published mentions/statements pointing at them
                    # are dropped downstream by the entity_id_map.get
                    # fallback. accept→normalize type (keep producer ckey
                    # so cross-peer matching still works).
                    try:
                        from core.ldu.subtype_validator import validate_for_ldu
                        _v = validate_for_ldu(etype)
                        if _v.action == "accept":
                            if _v.canonical_subtype and _v.canonical_subtype != etype:
                                etype = _v.canonical_subtype
                        else:
                            log.info(
                                "extraction_subscriber: skipping peer entity "
                                "%r (type=%r, action=%s)",
                                name, etype, _v.action,
                            )
                            continue
                    except Exception:
                        log.debug("subtype_validator failed for peer entity %r",
                                  name, exc_info=True)

                    # Resolve local entity (pick highest signal_count when dupes)
                    cur.execute(
                        """
                        SELECT entity_id FROM entities
                         WHERE tenant_id = %s AND canonical_entity_key = %s
                         ORDER BY COALESCE(signal_count, 0) DESC, entity_id ASC
                         LIMIT 1
                        """,
                        (tenant_id, ckey),
                    )
                    row = cur.fetchone()
                    if row:
                        entity_id_map[pid] = row[0]
                    else:
                        # CREATE with full GDU state from envelope. Producer
                        # already ran GDU admission; we trust their verdict
                        # subject to the verification gates above.
                        # normalized_name is NOT NULL on entities. Convention
                        # (matches node-A): lowercased + whitespace-collapsed.
                        # We re-derive locally rather than trusting producer.
                        import re as _re_norm
                        normalized = _re_norm.sub(r"\s+", " ", name.lower()).strip()
                        cur.execute(
                            """
                            INSERT INTO entities (
                                tenant_id, canonical_name, canonical_type,
                                normalized_name,
                                canonical_entity_key, base60_address,
                                signal_count, gdu_canonical,
                                admitted_under_pack_versions,
                                admitted_under_active_set,
                                identifiers, pulled_from_node_id,
                                created_at, updated_at
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s,
                                COALESCE(%s::jsonb, '{}'::jsonb),
                                %s, COALESCE(%s::jsonb, '{}'::jsonb),
                                %s, NOW(), NOW()
                            )
                            RETURNING entity_id
                            """,
                            (
                                tenant_id, name, etype, normalized[:500], ckey,
                                e.get("base60_address"),
                                int(e.get("signal_count") or 0),
                                bool(e.get("gdu_canonical")),
                                json.dumps(e.get("admitted_under_pack_versions") or {}),
                                # admitted_under_active_set is NOT NULL on
                                # entities; carry producer's value or mark
                                # pulled.
                                (e.get("admitted_under_active_set") or "pulled_v1")[:32],
                                json.dumps(e.get("identifiers") or {}),
                                producer_node_id,
                            ),
                        )
                        _new_eid = cur.fetchone()[0]
                        entity_id_map[pid] = _new_eid
                        rows_inserted["entities_new"] += 1

                        # Attestation (C3) — fires trg_mirror_entity, minting a
                        # knowledge_node. Same reasoning as library_subscriber:
                        # accepting a peer's entity IS an admission decision by
                        # this node, and which producer it came from cannot be
                        # reconstructed after the fact.
                        from core.gdu.attest import attest_admission
                        attest_admission(
                            conn, tenant_id,
                            operation="admit",
                            writer="workers.extraction_subscriber.pull_extractions",
                            checks_run={
                                # Producer payload. The verification gates above
                                # this block screen the EXTRACTION, not the
                                # (category, subtype) against the local pack.
                                "boundary": False,
                                "vocab": False,
                                # Producer's address, carried across rather than
                                # minted from this node's sequence.
                                "address": False,
                                # ckey lookup above is the dedup decision.
                                "collapse": True,
                            },
                            node_id=f"entity:{_new_eid}", entity_id=_new_eid,
                            category="entity", subtype=etype,
                            base60=e.get("base60_address"),
                            input_type=etype, mapped_type=etype,
                            reason=f"federation pull from {producer_node_id}"[:500],
                            lineage_stamp={
                                "active_set_version":
                                    (e.get("admitted_under_active_set") or "pulled_v1")[:32],
                                "pack_versions": e.get("admitted_under_pack_versions") or {},
                                "pulled_from_node_id": producer_node_id,
                            },
                        )

                # ─── Mentions → document_mentions + entity_mentions ───
                for m in mentions_payload:
                    pid = m.get("entity_id")  # producer-local PK
                    local_eid = entity_id_map.get(pid) if pid is not None else None
                    surface = (m.get("surface_form") or "")[:MAX_SURFACE_FORM_LEN]
                    if not surface:
                        continue  # mention must have a surface form

                    # Noise filter — peer-published mentions still get
                    # screened locally. If they match a known noise pattern
                    # we mark review_status='rejected' on insert; the row
                    # is preserved for audit but hidden from default views.
                    review_status = None
                    try:
                        from core.documents.noise_patterns import classify_noise
                        v = classify_noise(
                            surface,
                            (m.get("suggested_type") or "") or "",
                        )
                        if v.action in ("reject", "redaction_marker"):
                            review_status = "rejected"
                    except Exception:
                        pass

                    # document_mentions row — text span layer
                    if review_status:
                        cur.execute(
                            """
                            INSERT INTO document_mentions (
                                tenant_id, document_id, page_id, paragraph_id,
                                char_start, char_end, surface_form, surface_form_norm,
                                suggested_type, candidate_entity_id, confidence,
                                resolution_status, candidates,
                                extraction_method, extraction_engines,
                                pulled_from_node_id, pulled_from_envelope_sha,
                                review_status, reviewed_by, reviewed_at
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                COALESCE(%s::jsonb, NULL),
                                %s, COALESCE(%s::jsonb, NULL),
                                %s, %s,
                                %s, %s, NOW()
                            )
                            RETURNING mention_id
                            """,
                            (
                                tenant_id, document_id,
                                m.get("page_id"), m.get("paragraph_id"),
                                m.get("char_start"), m.get("char_end"),
                                surface, m.get("surface_form_norm"),
                                (m.get("suggested_type") or "")[:64] or None,
                                local_eid,
                                _clamp_confidence(m.get("confidence")),
                                (m.get("resolution_status") or "unresolved")[:32],
                                json.dumps(m.get("candidates")) if m.get("candidates") else None,
                                (m.get("extraction_method") or "")[:64] or None,
                                json.dumps(m.get("extraction_engines")) if m.get("extraction_engines") else None,
                                producer_node_id, envelope_sha,
                                review_status, "noise_filter_v1",
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO document_mentions (
                                tenant_id, document_id, page_id, paragraph_id,
                                char_start, char_end, surface_form, surface_form_norm,
                                suggested_type, candidate_entity_id, confidence,
                                resolution_status, candidates,
                                extraction_method, extraction_engines,
                                pulled_from_node_id, pulled_from_envelope_sha
                            ) VALUES (
                                %s, %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s,
                                COALESCE(%s::jsonb, NULL),
                                %s, COALESCE(%s::jsonb, NULL),
                                %s, %s
                            )
                            RETURNING mention_id
                            """,
                            (
                                tenant_id, document_id,
                                m.get("page_id"), m.get("paragraph_id"),
                                m.get("char_start"), m.get("char_end"),
                                surface, m.get("surface_form_norm"),
                                (m.get("suggested_type") or "")[:64] or None,
                                local_eid,
                                _clamp_confidence(m.get("confidence")),
                                (m.get("resolution_status") or "unresolved")[:32],
                                json.dumps(m.get("candidates")) if m.get("candidates") else None,
                                (m.get("extraction_method") or "")[:64] or None,
                                json.dumps(m.get("extraction_engines")) if m.get("extraction_engines") else None,
                                producer_node_id, envelope_sha,
                            ),
                        )
                    rows_inserted["document_mentions"] += 1

                    # entity_mentions row — resolved layer (requires non-null entity_id)
                    if local_eid is not None:
                        cur.execute(
                            """
                            INSERT INTO entity_mentions (
                                tenant_id, entity_id, signal_rid, node_id,
                                source_url, name_raw, role, context_snippet,
                                first_offset, confidence,
                                extraction_profile_id,
                                pulled_from_node_id, pulled_from_envelope_sha
                            ) VALUES (
                                %s, %s, %s, NULL,
                                NULL, %s, NULL, NULL,
                                %s, %s,
                                %s, %s, %s
                            )
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                tenant_id, local_eid, document_id,
                                surface,
                                m.get("char_start"),
                                _clamp_confidence(m.get("confidence")),
                                profile,
                                producer_node_id, envelope_sha,
                            ),
                        )
                        rows_inserted["entity_mentions"] += 1

                # ─── Statements ───
                for s in statements_payload:
                    pred = (s.get("predicate") or "")[:MAX_PREDICATE_LEN]
                    if not pred:
                        continue
                    cur.execute(
                        """
                        INSERT INTO statements (
                            tenant_id, document_id, page_id, paragraph_id,
                            speaker_entity_id, object_entity_id,
                            predicate, raw_text, summarized_text,
                            confidence,
                            extraction_method,
                            extraction_profile_id,
                            pulled_from_node_id, pulled_from_envelope_sha
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s,
                            %s, %s, %s,
                            %s,
                            %s,
                            %s,
                            %s, %s
                        )
                        """,
                        (
                            tenant_id, document_id,
                            s.get("page_id"), s.get("paragraph_id"),
                            entity_id_map.get(s.get("speaker_entity_id")),
                            entity_id_map.get(s.get("object_entity_id")),
                            pred,
                            (s.get("raw_text") or "")[:MAX_RAW_TEXT_LEN] or None,
                            (s.get("summarized_text") or "")[:MAX_RAW_TEXT_LEN] or None,
                            _clamp_confidence(s.get("confidence")),
                            "federation_pull",
                            profile,
                            producer_node_id, envelope_sha,
                        ),
                    )
                    rows_inserted["statements"] += 1

                # ─── Assessments ───
                # NOTE: assessments require base60_address (UNIQUE) and
                # collapse_key (UNIQUE). We mint local pulled-namespace
                # values so producer's address space doesn't collide.
                for idx, a in enumerate(assessments_payload):
                    edge = (a.get("predicate_edge_type") or "")[:128]
                    if not edge:
                        continue
                    subj = entity_id_map.get(a.get("subj_entity_id"))
                    obj  = entity_id_map.get(a.get("obj_entity_id"))
                    local_addr = f"pulled:{envelope_sha[:8]}:{idx:04d}"
                    local_ckey = hashlib.sha256(
                        f"{subj}|{edge}|{obj}|{envelope_sha[:8]}".encode("utf-8")
                    ).hexdigest()
                    cur.execute(
                        """
                        INSERT INTO assessments (
                            tenant_id, base60_address, assessment_tier,
                            subj_entity_id, predicate_edge_type, obj_entity_id,
                            obj_value, polarity, collapse_key,
                            corroborated_by_count, contradicted_by_count,
                            confidence, source_statement_ids,
                            earliest_event_time, latest_event_time,
                            first_seen, last_seen,
                            pulled_from_node_id, pulled_from_envelope_sha
                        ) VALUES (
                            %s, %s, COALESCE(%s, 'observation'),
                            %s, %s, %s,
                            %s, 'affirmative', %s,
                            1, 0,
                            %s, '{}'::bigint[],
                            %s, %s,
                            NOW(), NOW(),
                            %s, %s
                        )
                        ON CONFLICT (collapse_key) DO NOTHING
                        """,
                        (
                            tenant_id, local_addr,
                            (a.get("assessment_tier") or "observation")[:32],
                            subj, edge, obj,
                            (a.get("obj_value") or None),
                            local_ckey,
                            _clamp_confidence(a.get("confidence")),
                            a.get("earliest_event_time"),
                            a.get("latest_event_time"),
                            producer_node_id, envelope_sha,
                        ),
                    )
                    rows_inserted["assessments"] += 1

                # Stamp the doc so the local extractor doesn't re-run it
                cur.execute(
                    """
                    UPDATE documents
                       SET extraction_state = 'pulled_from_network'
                     WHERE document_id = %s
                    """,
                    (document_id,),
                )
            conn.commit()

    except Exception as e:
        log.warning("apply_extraction_envelope rolled back: %s", e)
        # Mark the doc as quarantined so future ticks don't re-attempt it
        # without operator intervention.
        try:
            from storage.postgres.db import get_connection as _gc
            with _gc() as conn2:
                with conn2.cursor() as cur2:
                    cur2.execute(
                        "UPDATE documents SET extraction_state = 'quarantine' WHERE document_id = %s",
                        (document_id,),
                    )
                conn2.commit()
        except Exception:
            pass
        return {"decision": "quarantined", "reason_code": "g9_tx_rolled_back",
                "reason_detail": str(e)[:300], "row_counts": rows_inserted, "rows": 0}

    total_rows = sum(rows_inserted.values())
    return {"decision": "accepted", "reason_code": "ok",
            "row_counts": rows_inserted, "rows": total_rows}


def _abort(code: str, detail: str) -> dict:
    return {"decision": "rejected", "reason_code": code, "reason_detail": detail,
            "row_counts": None, "rows": 0}


# ── Helpers ───────────────────────────────────────────────────────────────


def _canonical_key(name: str, etype: str) -> str:
    """Cross-node identity key for G8 envelope verification. Delegates to the
    single source (core.gdu.identity) so it can never drift from the
    trigger / publisher."""
    from core.gdu.identity import canonical_entity_key
    return canonical_entity_key(name, etype)


def _clamp_confidence(v) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f < 0.0:
        return 0.0
    if f > 1.0:
        return 1.0
    return f


def _audit(*, content_sha256: str, profile_fingerprint: Optional[str],
            producer_node_id: Optional[str],
            decision: str, reason_code: str,
            envelope_sha: Optional[str] = None,
            hub_claimed_sha: Optional[str] = None,
            reason_detail: Optional[str] = None,
            row_counts_applied: Optional[dict] = None,
            duration_ms: Optional[int] = None) -> None:
    """Append one row to extraction_pull_audit. Never raises."""
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extraction_pull_audit (
                        content_sha256, profile_fingerprint, producer_node_id,
                        envelope_sha, hub_claimed_sha,
                        decision, reason_code, reason_detail,
                        row_counts_applied, duration_ms
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        COALESCE(%s::jsonb, NULL), %s
                    )
                    """,
                    (
                        content_sha256,
                        profile_fingerprint or "unknown",
                        producer_node_id or "unknown",
                        envelope_sha, hub_claimed_sha,
                        decision, reason_code, reason_detail,
                        json.dumps(row_counts_applied) if row_counts_applied else None,
                        duration_ms,
                    ),
                )
            conn.commit()
    except Exception as e:
        log.warning("audit write failed (decision=%s code=%s): %s",
                    decision, reason_code, e)
