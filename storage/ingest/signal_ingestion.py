"""
Universal Signal Ingestion Pipeline (CC-3)

THE core function. Raw bytes in → governed, classified, chain-hashed signal out.

Pipeline stages:
  1. LDU-T: Store raw bytes (never fails, never rejects)
  2. CLASSIFY: Auto-detect format, select parser
  3. PARSE: Extract structured data + observables
  4. GDU CHECK: Signal type in vocabulary?
     YES → fast path to LDU-W (stage 5)
     NO  → LDU-P playground (stage 4B)
  4B. LDU-P: Stage provisional entry for review
  5. LDU-W: Store normalized signal
  6. OBSERVABLES: Index extracted observables
  7. CHAIN: Commit to signal chain, issue receipt
  8. AUDIT: Emit audit event into audit chain (cross-chain anchor)

Per FD-1: Signal chain is SEPARATE from audit chain.
Per FD-2: node_id stamped on every record.
Per FD-3: Signal pipeline lives alongside audit pipeline.
Per FD-6: IDs include node prefix.

Usage:
    from storage.ingest.signal_ingestion import ingest_signal, ingest_signal_batch

    result = ingest_signal(
        raw_bytes=b'{"temperature": 72}',
        device_id="sensor-01",
        tenant_id="my-tenant",
        content_type="application/json",
    )
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

from storage.signals.ids import generate_signal_id, generate_ldu_t_id, generate_ldu_p_id
from storage.signals.models import LduTSignal, Signal, LduPEntry, Observable
from storage.signals.store import (
    insert_ldu_t, insert_signal, insert_ldu_p, insert_observables,
    compute_signal_cid, get_ldu_p, get_ldu_t, resolve_ldu_p,
)
from storage.signals.chain import commit_signal_batch
from storage.ingest.parsers import detect_and_parse, ParseResult
from storage.signals.organize import auto_tag_signal

import logging
_log = logging.getLogger("enki.ingestion")

# Load signal type vocabulary once (lazy)
_SIGNAL_TYPES: Optional[set] = None
_SPINE_VALIDATED: bool = False


def _build_metadata(parse_result: ParseResult, signal_type: str, signal_id: str,
                     lat: float = None, lon: float = None) -> dict:
    """Build signal metadata including card recipe for UI rendering."""
    meta = {
        "parser": parse_result.parser_name,
        "format_detected": parse_result.format_detected,
        "detection_confidence": parse_result.confidence,
        "auto_tags": _auto_tag(signal_type, parse_result.parsed),
    }
    # Ensure card template exists for this signal type + render card data
    try:
        from storage.signals.card_templates import ensure_template, render_card
        template = ensure_template(signal_type, parse_result.parsed)
        meta["card_recipe"] = render_card(template, parse_result.parsed, signal_id, lat, lon)
    except Exception as e:
        import logging
        logging.getLogger("enki.ingest").warning("card_recipe generation failed: %s", e, exc_info=True)
        meta["card_recipe"] = None
    return meta


def _auto_tag(signal_type: str, payload: dict) -> list[str]:
    """Generate automatic tags for a signal."""
    try:
        return auto_tag_signal(signal_type, payload)
    except Exception:
        return []


def _get_node_id() -> str:
    """Get node_id (FD-2). Falls back to 'local' in dev/test."""
    return os.environ.get("ENKI_NODE_ID", "local")


def _load_signal_types() -> set:
    """Load valid signal types through GDU governance context.

    Uses the GDU-backed vocabulary loader which:
    1. Loads signalvocab through GDUContext with strict mode
    2. Verifies the pack hash against active_set_v4.json
    3. Returns canonical=True only if governance verification passes
    4. Falls back to filesystem if GDU context unavailable (logged as warning)

    The signal ingestion pipeline is now LOCKED BEHIND THE GDU:
    - Signal types can only be added via the pack admission ceremony
    - Modifying the JSON file on disk without updating the content hash
      will cause the GDU hash check to fail
    - Non-canonical loads are logged for audit but allowed (graceful degradation)
    """
    global _SIGNAL_TYPES, _SPINE_VALIDATED
    if _SIGNAL_TYPES is not None:
        return _SIGNAL_TYPES

    try:
        from core.gdu.signal_vocabulary import load_signal_types_via_gdu
        types, canonical, verification = load_signal_types_via_gdu()
        _SIGNAL_TYPES = types
        _SPINE_VALIDATED = canonical

        if canonical:
            _log.info(
                "GDU: signal vocabulary loaded CANONICALLY — %d types, "
                "active set v%s, hash verified",
                len(types), verification.get("active_set_version", "?"),
            )
        else:
            _log.warning(
                "GDU: signal vocabulary loaded via FALLBACK (non-canonical) — "
                "%d types. Method: %s. Error: %s",
                len(types),
                verification.get("method", "unknown"),
                verification.get("error", "none"),
            )
    except Exception as e:
        _log.error("GDU: signal vocabulary loading failed entirely — %s", e)
        _SIGNAL_TYPES = set()
        _SPINE_VALIDATED = False

    return _SIGNAL_TYPES


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _emit_audit_event(
    tenant_id: str,
    event_type: str,
    signal_id: str,
    signal_type: str,
    signal_rid: int,
    node_id: str,
    path: str,
    dsn: Optional[str] = None,
) -> None:
    """Emit an audit event into the audit_events table for signal lifecycle tracking.

    This creates the cross-chain anchor between the signal chain and the audit chain.
    Best-effort — audit emission failures never block signal ingestion.
    """
    try:
        from storage.postgres.db import get_connection

        now = datetime.now(timezone.utc)
        payload = {
            "event_type": event_type,
            "signal_id": signal_id,
            "signal_type": signal_type,
            "signal_rid": signal_rid,
            "path": path,
            "timestamp": now.isoformat(),
            "node_id": node_id,
        }
        cid = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO audit_events
                    (tenant_id, cid, payload_json, event_type, timestamp, node_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, cid) DO NOTHING
                """, (
                    tenant_id, cid, json.dumps(payload),
                    event_type, now, node_id,
                ))
            conn.commit()
    except Exception as e:
        _log.debug("Audit event emission failed (non-blocking): %s", e)


@dataclass
class IngestionResult:
    """Result of ingesting a single signal."""
    success: bool
    ldu_t_id: str                                  # always set (raw always stored)
    signal_id: Optional[str] = None                # set if LDU-W stored
    ldu_p_id: Optional[str] = None                 # set if LDU-P staged
    signal_rid: Optional[int] = None               # DB rid if stored
    format_detected: str = ""
    confidence: float = 0.0
    signal_type: Optional[str] = None
    path: str = ""                                 # "ldu_w" or "ldu_p" or "ldu_t_only"
    observables_count: int = 0
    error: Optional[str] = None


@dataclass
class BatchIngestionResult:
    """Result of ingesting a batch of signals."""
    total: int
    accepted: int                                  # stored in LDU-W
    provisioned: int                               # staged in LDU-P
    stored_raw: int                                # stored in LDU-T only (parse failed)
    results: list[IngestionResult] = field(default_factory=list)
    chain_hash: Optional[str] = None
    receipt_rid: Optional[int] = None


def _update_paired_device(device_id: str, tenant_id: str, dsn: Optional[str] = None) -> None:
    """Update paired_devices signals_count and last_seen_at on every ingestion."""
    try:
        from storage.postgres.db import get_connection
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE paired_devices SET signals_count = signals_count + 1, "
                    "last_seen_at = NOW() WHERE device_id = %s AND tenant_id = %s",
                    (device_id, tenant_id),
                )
            conn.commit()
    except Exception:
        pass  # best-effort, don't block ingestion


def ingest_signal(
    raw_bytes: bytes,
    device_id: str,
    tenant_id: str,
    content_type: Optional[str] = None,
    source_type: str = "unknown",
    identity_id: Optional[str] = None,
    device_signature: Optional[str] = None,
    dsn: Optional[str] = None,
    signal_type_hint: Optional[str] = None,
    topic_id: Optional[str] = None,
) -> IngestionResult:
    """
    Ingest a single signal through the full pipeline.

    This is the MAIN ENTRY POINT for all data entering Enki.

    Args:
        raw_bytes: Raw payload (any format)
        device_id: Source device identifier
        tenant_id: Tenant scope
        content_type: MIME type hint (optional)
        source_type: Source type from signalvocab (e.g., "http_api", "phone_app")
        identity_id: Who/what generated this (optional)
        device_signature: Ed25519 signature from device (optional)
        dsn: Database connection string (optional)

    Returns:
        IngestionResult with LDU-T ID, signal ID (if classified), LDU-P ID (if staged)
    """
    node_id = _get_node_id()
    raw_sha256 = _sha256(raw_bytes)

    # ─── STAGE 1: LDU-T — Store raw bytes (NEVER fails) ───────────────
    ldu_t_id = generate_ldu_t_id(node_id)

    ldu_t_entry = LduTSignal(
        ldu_t_id=ldu_t_id,
        tenant_id=tenant_id,
        node_id=node_id,
        device_id=device_id,
        source_type=source_type,
        raw_bytes=raw_bytes,
        raw_sha256=raw_sha256,
        raw_byte_length=len(raw_bytes),
        content_type=content_type,
    )

    ldu_t_rid = insert_ldu_t(ldu_t_entry, dsn)
    if ldu_t_rid == -1:
        # Duplicate payload (same tenant + same SHA-256)
        return IngestionResult(
            success=True,
            ldu_t_id=ldu_t_id,
            path="duplicate",
            format_detected="",
            error="Duplicate payload (same content hash already stored)",
        )

    # ─── FAST-PATH: Device Profile Check ──────────────────────────────
    # If we already know this device's payload shape, skip classify/parse
    # and go straight to LDU-W using the stored field mapping.
    try:
        from storage.signals.device_profiles import (
            lookup_device_profile, apply_profile, increment_signal_count,
        )
        profile = lookup_device_profile(device_id, tenant_id, dsn)
        if profile and profile.get("status") == "active":
            fast_result = _apply_profile_fast_path(
                profile=profile,
                raw_bytes=raw_bytes,
                ldu_t_id=ldu_t_id,
                device_id=device_id,
                tenant_id=tenant_id,
                identity_id=identity_id,
                source_type=source_type,
                device_signature=device_signature,
                dsn=dsn,
            )
            if fast_result is not None:
                increment_signal_count(device_id, tenant_id, dsn)
                return fast_result
    except Exception:
        pass  # Fall through to normal pipeline on any error

    # ─── STAGE 2+3: CLASSIFY + PARSE ──────────────────────────────────
    try:
        parse_result = detect_and_parse(raw_bytes, content_type, device_id)
    except Exception as e:
        # Parse crashed — raw bytes still safe in LDU-T
        return IngestionResult(
            success=True,
            ldu_t_id=ldu_t_id,
            path="ldu_t_only",
            format_detected="parse_error",
            error=f"Parser error: {e}",
        )

    # Update LDU-T with detection results (format + confidence)
    # (We could update the row, but LDU-T is append-only by trigger.
    #  Detection results are captured in the signal/LDU-P record instead.)

    # ─── STAGE 4: GDU VOCABULARY CHECK ────────────────────────────────
    # Use connector-provided hint if the parser didn't detect one
    signal_type = parse_result.signal_type_hint or signal_type_hint
    valid_types = _load_signal_types()

    type_in_vocabulary = signal_type in valid_types if signal_type else False

    if not type_in_vocabulary:
        # ─── STAGE 4B: LDU-P — Playground ─────────────────────────────
        # Also raise an alert to the user via unknown_signal_types so
        # they can Ignore or Promote. One row per novel signal_type per
        # tenant (incremented, not duplicated, on subsequent arrivals).
        try:
            _record_unknown_signal_type(
                tenant_id=tenant_id,
                signal_type=signal_type or "unknown",
                payload_sample=parse_result.parsed if parse_result else None,
                dsn=dsn,
            )
        except Exception as _e:
            # Alert registration is best-effort; never break ingestion
            logging.getLogger("enki.ingest.unknown_type").debug(
                "unknown_signal_types upsert swallowed error: %s", _e
            )
        return _stage_in_ldu_p(
            ldu_t_id=ldu_t_id,
            tenant_id=tenant_id,
            node_id=node_id,
            parse_result=parse_result,
            dsn=dsn,
        )

    # ─── STAGE 5: LDU-W — Store normalized signal ─────────────────────
    signal_id = generate_signal_id(node_id)
    now = datetime.now(timezone.utc)

    # ─── PRE-INSERT FINANCIAL ENRICHMENT ──────────────────────────────
    # Enrich financial payloads BEFORE insert (signals table is append-only).
    financial_meta = {}
    if signal_type and signal_type.startswith("signal.financial."):
        try:
            from core.enrichment.financial_enrich import enrich_payload_before_insert
            parse_result.parsed, financial_meta = enrich_payload_before_insert(
                parse_result.parsed, signal_type
            )
        except Exception:
            pass  # enrichment failures don't block ingestion

    # Extract location from parsed data (multiple strategies)
    parsed = parse_result.parsed
    lat = parsed.get("lat") or parsed.get("latitude")
    lon = parsed.get("lon") or parsed.get("longitude")
    alt = parsed.get("hae") or parsed.get("altitude")

    # Strategy 2: GeoJSON Feature format (USGS earthquakes, NWS alerts, etc.)
    if lat is None and lon is None:
        geom = parsed.get("geometry") or {}
        if isinstance(geom, dict) and geom.get("type") == "Point":
            coords = geom.get("coordinates", [])
            if len(coords) >= 2:
                lon, lat = coords[0], coords[1]  # GeoJSON is [lon, lat]
                if len(coords) >= 3 and alt is None:
                    alt = coords[2]

    # Strategy 3: Nested in properties (some APIs put coords there)
    if lat is None and lon is None:
        props = parsed.get("properties") or {}
        if isinstance(props, dict):
            lat = props.get("lat") or props.get("latitude")
            lon = props.get("lon") or props.get("longitude")

    # Extract timestamp from parsed data
    ts_str = parse_result.parsed.get("time") or parse_result.parsed.get("timestamp")
    try:
        signal_timestamp = datetime.fromisoformat(ts_str) if ts_str else now
    except (ValueError, TypeError):
        signal_timestamp = now

    # Ensure timezone-aware
    if signal_timestamp.tzinfo is None:
        signal_timestamp = signal_timestamp.replace(tzinfo=timezone.utc)

    signal = Signal(
        signal_id=signal_id,
        tenant_id=tenant_id,
        node_id=node_id,
        device_id=device_id,
        identity_id=identity_id,
        signal_type=signal_type,
        source_domain=parse_result.metadata.get("source_hint", source_type),
        severity="informational",
        confidence="confirmed" if parse_result.confidence >= 0.9 else
                   "probable" if parse_result.confidence >= 0.7 else "possible",
        timestamp=signal_timestamp,
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
        altitude=float(alt) if alt is not None else None,
        payload=parse_result.parsed,
        observables=[o.model_dump() for o in parse_result.observables],
        metadata={
            **_build_metadata(
                parse_result, signal_type, signal_id,
                float(lat) if lat is not None else None,
                float(lon) if lon is not None else None,
            ),
            **financial_meta,
        },
        ldu_t_id=ldu_t_id,
        cid="placeholder",  # computed below
        origin_node_id=node_id,
        route_path=[node_id],
        device_signature=device_signature,
    )

    # Compute CID from content
    signal.cid = compute_signal_cid(signal)

    # Tag signal metadata with GDU governance status
    signal.metadata["gdu_canonical"] = _SPINE_VALIDATED

    # ─── FACET EXTRACTION (Tier 1 — fast keyword/regex) ──────────────
    try:
        from storage.signals.facets import extract_facets, harvest_candidates, resolve_facet_entities
        facets = extract_facets(signal_type, parse_result.parsed, signal_id)
        if facets and len(facets) > 1:  # more than just {"tier": 1}
            # Resolve facet strings to canonical entity IDs (learning layer)
            try:
                facets = resolve_facet_entities(facets, tenant_id, dsn, source_signal_id=signal_id)
            except Exception as _re:
                _log.debug("Entity resolution skipped: %s", _re)

            signal.metadata["facets"] = facets

            # Harvest low-confidence entities as candidates for LLM review
            try:
                headline = (parse_result.parsed.get("title")
                            or parse_result.parsed.get("headline") or "")
                source = (parse_result.parsed.get("source")
                          or parse_result.parsed.get("feed_title") or "")
                if headline:
                    harvest_candidates(facets, signal_id, headline, source, tenant_id, dsn)
            except Exception:
                pass
    except Exception as _fe:
        _log.debug("Facet extraction skipped (non-blocking): %s", _fe)

    # If the connector tagged this signal with a watch_topic_id
    # (set by watch_topics._auto_create_connectors via source_config._topic_id),
    # stamp it into metadata so the downstream research fanout hook can
    # attribute the signal to its originating topic without keyword matching.
    if topic_id:
        signal.metadata["_topic_id"] = topic_id

    signal_rid = insert_signal(signal, dsn)

    # ─── FACET COUNT AGGREGATION ─────────────────────────────────────
    try:
        facets = signal.metadata.get("facets")
        if facets:
            from storage.signals.facets import increment_facet_counts, update_entity_mentions
            increment_facet_counts(
                facets=facets,
                signal_id=signal_id,
                tenant_id=tenant_id,
                timestamp=signal_timestamp,
                dsn=dsn,
            )
            # Update entity mention counts and signal_extractions link
            update_entity_mentions(
                facets=facets,
                signal_id=signal_id,
                timestamp=signal_timestamp,
                dsn=dsn,
            )
    except Exception as _fce:
        _log.debug("Facet count increment skipped (non-blocking): %s", _fce)

    # ─── STAGE 8: AUDIT EVENT — Cross-chain anchor ────────────────────
    _emit_audit_event(
        tenant_id=tenant_id,
        event_type="signal.ingested",
        signal_id=signal_id,
        signal_type=signal_type,
        signal_rid=signal_rid,
        node_id=node_id,
        path="ldu_w",
        dsn=dsn,
    )

    # ─── STAGE 6: OBSERVABLE INDEX ─────────────────────────────────────
    obs_count = insert_observables(
        tenant_id=tenant_id,
        signal_rid=signal_rid,
        observables=parse_result.observables,
        dsn=dsn,
    )

    # ─── STAGE 7: CORRELATION + DETECTION ──────────────────────────────
    # Check observables against case watchlists and evaluate detection rules.
    # This is best-effort — correlation/detection failures don't block ingestion.
    alerts_generated = []
    try:
        alerts_generated = _run_correlation_and_detection(
            tenant_id=tenant_id,
            signal_rid=signal_rid,
            signal_type=signal_type,
            signal_dict=signal.model_dump(),
            observables=parse_result.observables,
            dsn=dsn,
        )
    except Exception as e:
        import logging
        logging.getLogger("enki.ingestion").warning(
            f"Correlation/detection error for signal {signal_id}: {e}"
        )

    # ─── Auto-profile: track consistent payloads for this device ────
    try:
        from storage.signals.device_profiles import maybe_auto_create_profile
        maybe_auto_create_profile(
            device_id=device_id,
            tenant_id=tenant_id,
            parsed=parse_result.parsed,
            signal_type=signal_type,
            parser_name=parse_result.parser_name,
            dsn=dsn,
        )
    except Exception:
        pass  # auto-profile failures don't block ingestion

    # ─── BATCH TRANSACTION SPLITTING ──────────────────────────────────
    # Bank statements contain multiple transactions in payload.transactions[].
    # After storing the parent signal, split each transaction into its own signal.
    if signal_type == "signal.financial.transaction" and isinstance(parsed.get("transactions"), list):
        transactions = parsed["transactions"]
        if len(transactions) > 1:
            _split_transactions(
                transactions=transactions,
                parent_signal_id=signal_id,
                parent_ldu_t_id=ldu_t_id,
                device_id=device_id,
                tenant_id=tenant_id,
                identity_id=identity_id,
                source_type=source_type,
                account_info=parsed.get("account", {}),
                dsn=dsn,
            )

    # Update paired device counter
    _update_paired_device(device_id, tenant_id, dsn)

    # ─── STAGE 9: UNIVERSAL EXTRACTION ────────────────────────────────
    # Route the stored signal through the extraction runner so it
    # populates signal_extractions, resolves entities against the
    # canonical tables, runs the reality-check validator, scores
    # completeness, fans out to matching watch topics, and upserts
    # knowledge_nodes + knowledge_edges. Non-blocking.
    try:
        from storage.ingest.extraction_dispatch import dispatch_extraction
        dispatch_extraction(
            signal_id=signal_id,
            signal_rid=signal_rid,
            signal_type=signal_type,
            payload=parsed,
            metadata=signal.metadata,
            tenant_id=tenant_id,
            source_name=signal.metadata.get("outlet_name", "") or source_type,
            language=signal.metadata.get("language", "en") or "en",
            dsn=dsn,
        )
    except Exception as _ede:
        _log.debug("extraction dispatch skipped (non-blocking): %s", _ede)

    return IngestionResult(
        success=True,
        ldu_t_id=ldu_t_id,
        signal_id=signal_id,
        signal_rid=signal_rid,
        format_detected=parse_result.format_detected,
        confidence=parse_result.confidence,
        signal_type=signal_type,
        path="ldu_w",
        observables_count=obs_count,
    )


def _apply_profile_fast_path(
    profile: dict,
    raw_bytes: bytes,
    ldu_t_id: str,
    device_id: str,
    tenant_id: str,
    identity_id: Optional[str],
    source_type: str,
    device_signature: Optional[str],
    dsn: Optional[str] = None,
) -> Optional[IngestionResult]:
    """Apply a device profile to skip classify/parse. Returns IngestionResult or None on error."""
    from storage.signals.device_profiles import apply_profile

    try:
        mapped = apply_profile(profile, raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None  # can't apply profile, fall through to normal pipeline

    # Prefer signal_type from payload if present and valid (desktop agents send typed signals)
    signal_type = mapped.get("signal_type") or profile.get("type_mapping", {}).get("signal_type")
    if not signal_type:
        return None

    # Verify signal type is still in vocabulary
    valid_types = _load_signal_types()
    if signal_type not in valid_types:
        # Fallback to profile type if payload type isn't in vocab
        signal_type = profile.get("type_mapping", {}).get("signal_type")
        if not signal_type or signal_type not in valid_types:
            return None

    node_id = _get_node_id()
    signal_id = generate_signal_id(node_id)
    now = datetime.now(timezone.utc)

    # Extract location from mapped data
    lat = mapped.get("latitude") or mapped.get("lat")
    lon = mapped.get("longitude") or mapped.get("lon")
    alt = mapped.get("altitude") or mapped.get("hae") or mapped.get("alt")

    # Extract timestamp
    ts_str = mapped.get("timestamp") or mapped.get("time")
    try:
        signal_timestamp = datetime.fromisoformat(ts_str) if ts_str else now
    except (ValueError, TypeError):
        signal_timestamp = now
    if signal_timestamp.tzinfo is None:
        signal_timestamp = signal_timestamp.replace(tzinfo=timezone.utc)

    severity = profile.get("type_mapping", {}).get("severity", "informational")
    source_domain = profile.get("type_mapping", {}).get("source_domain", source_type)

    signal = Signal(
        signal_id=signal_id,
        tenant_id=tenant_id,
        node_id=node_id,
        device_id=device_id,
        identity_id=identity_id,
        signal_type=signal_type,
        source_domain=source_domain,
        severity=severity,
        confidence="confirmed",
        timestamp=signal_timestamp,
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
        altitude=float(alt) if alt is not None else None,
        payload=mapped,
        observables=[],
        metadata=_build_metadata_from_profile(profile, signal_type, signal_id, mapped,
                                               float(lat) if lat is not None else None,
                                               float(lon) if lon is not None else None),
        ldu_t_id=ldu_t_id,
        cid="placeholder",
        origin_node_id=node_id,
        route_path=[node_id],
        device_signature=device_signature,
    )

    signal.cid = compute_signal_cid(signal)
    signal_rid = insert_signal(signal, dsn)

    # Audit event for fast-path
    _emit_audit_event(
        tenant_id=tenant_id,
        event_type="signal.ingested",
        signal_id=signal_id,
        signal_type=signal_type,
        signal_rid=signal_rid,
        node_id=node_id,
        path="ldu_w_fast",
        dsn=dsn,
    )

    # Extract observables from mapped data
    try:
        from storage.ingest.parsers.json_parser import _extract_observables_from_dict
        observables = _extract_observables_from_dict(mapped)
        obs_count = insert_observables(
            tenant_id=tenant_id,
            signal_rid=signal_rid,
            observables=observables,
            dsn=dsn,
        )
    except Exception:
        obs_count = 0

    # Run correlation and detection (best-effort)
    try:
        _run_correlation_and_detection(
            tenant_id=tenant_id,
            signal_rid=signal_rid,
            signal_type=signal_type,
            signal_dict=signal.model_dump(),
            observables=observables if obs_count else [],
            dsn=dsn,
        )
    except Exception as e:
        import logging
        logging.getLogger("enki.ingestion").warning(
            f"Fast-path correlation/detection error for {signal_id}: {e}"
        )

    # Update paired device counter
    _update_paired_device(device_id, tenant_id, dsn)

    # Route the stored signal through the universal extraction runner.
    try:
        from storage.ingest.extraction_dispatch import dispatch_extraction
        dispatch_extraction(
            signal_id=signal_id,
            signal_rid=signal_rid,
            signal_type=signal_type,
            payload=mapped if isinstance(mapped, dict) else {},
            metadata=signal.metadata,
            tenant_id=tenant_id,
            source_name=signal.metadata.get("outlet_name", "") or source_type,
            language=signal.metadata.get("language", "en") or "en",
            dsn=dsn,
        )
    except Exception as _ede:
        import logging
        logging.getLogger("enki.ingestion").debug(
            "fast-path extraction dispatch skipped: %s", _ede
        )

    return IngestionResult(
        success=True,
        ldu_t_id=ldu_t_id,
        signal_id=signal_id,
        signal_rid=signal_rid,
        format_detected=profile.get("payload_format", "json"),
        confidence=1.0,
        signal_type=signal_type,
        path="ldu_w_fast",
        observables_count=obs_count,
    )


def _build_metadata_from_profile(
    profile: dict, signal_type: str, signal_id: str, payload: dict,
    lat: float = None, lon: float = None,
) -> dict:
    """Build signal metadata using profile info + card template."""
    meta = {
        "parser": "device_profile",
        "format_detected": profile.get("payload_format", "json"),
        "detection_confidence": 1.0,
        "device_profile_id": profile.get("profile_id"),
        "auto_tags": _auto_tag(signal_type, payload),
    }
    try:
        from storage.signals.card_templates import ensure_template, render_card
        template = ensure_template(signal_type, payload)
        meta["card_recipe"] = render_card(template, payload, signal_id, lat, lon)
    except Exception:
        meta["card_recipe"] = None
    return meta


def _stage_in_ldu_p(
    ldu_t_id: str,
    tenant_id: str,
    node_id: str,
    parse_result: ParseResult,
    dsn: Optional[str] = None,
) -> IngestionResult:
    """Stage an unrecognized signal in LDU-P for review."""
    ldu_p_id = generate_ldu_p_id(node_id)

    # Determine vocabulary gaps
    gaps = []
    if parse_result.signal_type_hint:
        gaps.append(parse_result.signal_type_hint)

    entry = LduPEntry(
        ldu_p_id=ldu_p_id,
        tenant_id=tenant_id,
        ldu_t_id=ldu_t_id,
        format_detected=parse_result.format_detected,
        detection_confidence=parse_result.confidence,
        parser_used=parse_result.parser_name,
        extracted_structure=parse_result.parsed,
        proposed_mapping={
            "signal_type_proposed": parse_result.signal_type_hint,
            "observables_proposed": [o.model_dump() for o in parse_result.observables],
        },
        proposed_signal_type=parse_result.signal_type_hint,
        vocabulary_gaps=gaps,
    )

    insert_ldu_p(entry, dsn)

    # Audit: provisional signal staged
    _emit_audit_event(
        tenant_id=tenant_id,
        event_type="signal.staged",
        signal_id=ldu_p_id,
        signal_type=parse_result.signal_type_hint or "unknown",
        signal_rid=0,
        node_id=node_id,
        path="ldu_p",
        dsn=dsn,
    )

    return IngestionResult(
        success=True,
        ldu_t_id=ldu_t_id,
        ldu_p_id=ldu_p_id,
        format_detected=parse_result.format_detected,
        confidence=parse_result.confidence,
        signal_type=parse_result.signal_type_hint,
        path="ldu_p",
    )


def _split_transactions(
    transactions: list[dict],
    parent_signal_id: str,
    parent_ldu_t_id: str,
    device_id: str,
    tenant_id: str,
    identity_id: Optional[str],
    source_type: str,
    account_info: dict,
    dsn: Optional[str] = None,
) -> int:
    """
    Split a bank statement's transactions into individual signals.

    Each transaction in the parsed payload becomes its own signal.financial.transaction
    with the parent statement's signal_id in metadata for lineage.

    Returns count of child signals created.
    """
    node_id = _get_node_id()
    count = 0

    for txn in transactions[:5000]:  # safety cap
        # Build per-transaction payload
        txn_payload = {**txn}
        if account_info:
            txn_payload["account"] = account_info
        txn_payload["_parent_signal_id"] = parent_signal_id

        txn_bytes = json.dumps(txn_payload).encode("utf-8")

        try:
            result = ingest_signal(
                raw_bytes=txn_bytes,
                device_id=device_id,
                tenant_id=tenant_id,
                content_type="application/json",
                source_type=source_type,
                identity_id=identity_id,
                dsn=dsn,
                signal_type_hint="signal.financial.transaction",
            )
            if result.path == "ldu_w":
                count += 1
        except Exception:
            pass  # individual txn failures don't block others

    return count


def ingest_signal_batch(
    items: list[dict],
    tenant_id: str,
    dsn: Optional[str] = None,
) -> BatchIngestionResult:
    """
    Ingest a batch of signals and commit to signal chain.

    Each item dict should have:
        raw_bytes: bytes
        device_id: str
        content_type: str (optional)
        source_type: str (optional)

    After all signals are processed, commits the batch to the signal
    chain (FD-1) and returns the chain hash.
    """
    node_id = _get_node_id()
    results = []
    signal_rids = []
    signal_cids = []
    accepted = 0
    provisioned = 0
    stored_raw = 0

    for item in items:
        result = ingest_signal(
            raw_bytes=item["raw_bytes"],
            device_id=item.get("device_id", "unknown"),
            tenant_id=tenant_id,
            content_type=item.get("content_type"),
            source_type=item.get("source_type", "unknown"),
            identity_id=item.get("identity_id"),
            dsn=dsn,
            signal_type_hint=item.get("signal_type_hint"),
            topic_id=item.get("_topic_id"),
        )
        results.append(result)

        if result.path == "ldu_w" and result.signal_rid:
            signal_rids.append(result.signal_rid)
            # Use CID from the signal for chain
            signal_cids.append(result.signal_type or result.ldu_t_id)
            accepted += 1
        elif result.path == "ldu_p":
            provisioned += 1
        else:
            stored_raw += 1

    # ─── STAGE 7: SIGNAL CHAIN ─────────────────────────────────────────
    chain_result = None
    if signal_rids:
        chain_result = commit_signal_batch(
            tenant_id=tenant_id,
            node_id=node_id,
            signal_rids=signal_rids,
            signal_cids=signal_cids,
            dsn=dsn,
        )

    return BatchIngestionResult(
        total=len(items),
        accepted=accepted,
        provisioned=provisioned,
        stored_raw=stored_raw,
        results=results,
        chain_hash=chain_result["chain_hash"] if chain_result else None,
        receipt_rid=chain_result["receipt_rid"] if chain_result else None,
    )


def _run_correlation_and_detection(
    tenant_id: str,
    signal_rid: int,
    signal_type: str,
    signal_dict: dict,
    observables: list,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Run observable correlation and detection rules against a signal.
    Returns list of generated alerts."""
    from core.detection.correlation import correlate_signal
    from core.detection.rules import evaluate_rules
    from core.detection.alerts import generate_alert

    alerts = []

    # 1. Observable correlation — check all observables against case watchlists
    matches = correlate_signal(tenant_id, signal_rid, observables, dsn)
    for match in matches:
        alert = generate_alert(tenant_id, match, signal_rid, "correlation_engine", dsn)
        if alert:
            alerts.append(alert)

    # 2. Detection rules — evaluate all active rules against this signal
    signal_dict["rid"] = signal_rid
    signal_dict["signal_type"] = signal_type
    triggered = evaluate_rules(tenant_id, signal_dict, dsn)
    for trigger in triggered:
        alert = generate_alert(tenant_id, trigger, signal_rid, "detection_engine", dsn)
        if alert:
            alerts.append(alert)

    return alerts


# ─── LDU-P Promotion ────────────────────────────────────────────────────────

def promote_ldu_p(
    ldu_p_id: str,
    tenant_id: str,
    signal_type: str,
    resolved_by: str = "operator",
    dsn: Optional[str] = None,
) -> IngestionResult:
    """
    Promote a provisional LDU-P entry to a full LDU-W signal.

    Takes the extracted_structure from the LDU-P entry, wraps it into a
    Signal with the confirmed signal_type, stores it, indexes observables,
    and marks the LDU-P entry as promoted with lineage.

    Args:
        ldu_p_id: The LDU-P entry to promote
        tenant_id: Tenant scope
        signal_type: Confirmed GDU signal type (e.g. "signal.device.sensor_reading")
        resolved_by: Who approved the promotion
        dsn: Optional database DSN override

    Returns:
        IngestionResult with the new signal_id and path="promoted"
    """
    import logging
    log = logging.getLogger("enki.ingest.promote")

    # 1. Fetch the LDU-P entry
    entry = get_ldu_p(ldu_p_id, tenant_id, dsn)
    if not entry:
        return IngestionResult(
            success=False, ldu_t_id="", path="error",
            error=f"LDU-P entry {ldu_p_id} not found",
        )
    if entry["resolution"] != "pending_review":
        return IngestionResult(
            success=False, ldu_t_id=entry["ldu_t_id"], path="error",
            error=f"LDU-P entry already resolved: {entry['resolution']}",
        )

    # 2. Fetch the LDU-T record for lineage
    ldu_t = get_ldu_t(entry["ldu_t_id"], tenant_id, dsn)
    if not ldu_t:
        return IngestionResult(
            success=False, ldu_t_id=entry["ldu_t_id"], path="error",
            error=f"LDU-T record {entry['ldu_t_id']} not found",
        )

    # 3. Build the Signal from extracted structure
    node_id = ldu_t.get("node_id", _get_node_id())
    device_id = ldu_t.get("device_id", "unknown")
    payload = entry.get("extracted_structure", {})
    proposed = entry.get("proposed_mapping", {})
    observables_raw = proposed.get("observables_proposed", [])

    signal_id = generate_signal_id(node_id)
    cid = compute_signal_cid(payload)

    # Extract coordinates if present
    lat = payload.get("latitude") or payload.get("lat")
    lon = payload.get("longitude") or payload.get("lon")

    # Build metadata with card recipe
    class _FakeParseResult:
        parser_name = entry.get("parser_used", "promoted")
        format_detected = entry.get("format_detected", "json")
        confidence = entry.get("detection_confidence", 0.0) or 0.0
        parsed = payload

    meta = _build_metadata(_FakeParseResult(), signal_type, signal_id, lat, lon)

    signal = Signal(
        signal_id=signal_id,
        tenant_id=tenant_id,
        node_id=node_id,
        device_id=device_id,
        signal_type=signal_type,
        source_domain=ldu_t.get("source_type", "promoted"),
        severity="informational",
        confidence="unverified",
        timestamp=datetime.fromisoformat(entry["created_at"]),
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
        payload=payload,
        observables=[],
        metadata=meta,
        ldu_t_id=entry["ldu_t_id"],
        ldu_p_id=ldu_p_id,
        cid=cid,
        origin_node_id=node_id,
    )

    # 4. Store the signal (LDU-W)
    try:
        signal_rid = insert_signal(signal, dsn)
    except Exception as e:
        log.error("Failed to insert promoted signal: %s", e)
        return IngestionResult(
            success=False, ldu_t_id=entry["ldu_t_id"],
            ldu_p_id=ldu_p_id, path="error",
            error=f"Signal insertion failed: {e}",
        )

    # 5. Index observables
    obs_count = 0
    if observables_raw:
        try:
            obs_objects = [
                Observable(**o) if isinstance(o, dict) else o
                for o in observables_raw
            ]
            insert_observables(signal_id, signal_rid, tenant_id, obs_objects, dsn)
            obs_count = len(obs_objects)
        except Exception as e:
            log.warning("Observable indexing failed during promotion: %s", e)

    # 6. Mark LDU-P as promoted with lineage
    resolve_ldu_p(
        ldu_p_id=ldu_p_id,
        tenant_id=tenant_id,
        resolution="promoted",
        resolution_path=f"manual:{signal_type}",
        resolved_by=resolved_by,
        promoted_signal_rid=signal_rid,
        dsn=dsn,
    )

    # Audit: signal promoted from LDU-P
    _emit_audit_event(
        tenant_id=tenant_id,
        event_type="signal.promoted",
        signal_id=signal_id,
        signal_type=signal_type,
        signal_rid=signal_rid,
        node_id=node_id,
        path="promoted",
        dsn=dsn,
    )

    log.info("Promoted %s → signal %s (type=%s, rid=%d)",
             ldu_p_id, signal_id, signal_type, signal_rid)

    return IngestionResult(
        success=True,
        ldu_t_id=entry["ldu_t_id"],
        signal_id=signal_id,
        ldu_p_id=ldu_p_id,
        signal_rid=signal_rid,
        format_detected=entry.get("format_detected", ""),
        confidence=entry.get("detection_confidence", 0.0) or 0.0,
        signal_type=signal_type,
        path="promoted",
        observables_count=obs_count,
    )


def reject_ldu_p(
    ldu_p_id: str,
    tenant_id: str,
    reason: str = "",
    resolved_by: str = "operator",
    dsn: Optional[str] = None,
) -> bool:
    """Reject a provisional LDU-P entry. Returns True if updated."""
    return resolve_ldu_p(
        ldu_p_id=ldu_p_id,
        tenant_id=tenant_id,
        resolution="rejected",
        resolution_path=f"manual_reject:{reason}" if reason else "manual_reject",
        resolved_by=resolved_by,
        dsn=dsn,
    )


# ─── Unknown signal type registry (Phase B) ─────────────────────────────────

def _record_unknown_signal_type(
    *,
    tenant_id: str,
    signal_type: str,
    payload_sample: Optional[dict],
    dsn: Optional[str] = None,
) -> None:
    """Upsert a row in unknown_signal_types. One row per novel type per
    tenant, incremented on every arrival.

    The first N payload samples are stored (capped at 5) so the user
    can see what the data actually looks like when deciding Ignore /
    Promote in the Alerts tab.
    """
    from storage.postgres.db import get_connection
    import json as _json

    sample_json = None
    if isinstance(payload_sample, dict):
        # Redact any obviously-long values before storing
        safe = {}
        for k, v in list(payload_sample.items())[:30]:
            if isinstance(v, str) and len(v) > 400:
                safe[k] = v[:400] + "…"
            else:
                safe[k] = v
        sample_json = _json.dumps(safe, default=str)

    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # Upsert on (tenant_id, signal_type)
                cur.execute(
                    """
                    INSERT INTO unknown_signal_types
                        (tenant_id, signal_type, status, signal_count,
                         sample_payloads, first_seen_at, last_seen_at)
                    VALUES (%s, %s, 'pending', 1,
                            CASE WHEN %s IS NULL THEN '[]'::jsonb
                                 ELSE jsonb_build_array(%s::jsonb) END,
                            NOW(), NOW())
                    ON CONFLICT (tenant_id, signal_type) DO UPDATE
                        SET signal_count  = unknown_signal_types.signal_count + 1,
                            last_seen_at  = NOW(),
                            sample_payloads = CASE
                                WHEN jsonb_array_length(unknown_signal_types.sample_payloads) < 5
                                     AND %s IS NOT NULL
                                THEN unknown_signal_types.sample_payloads || jsonb_build_array(%s::jsonb)
                                ELSE unknown_signal_types.sample_payloads
                            END
                    """,
                    (tenant_id, signal_type, sample_json, sample_json, sample_json, sample_json),
                )
            conn.commit()
    except Exception as e:
        logger = logging.getLogger("enki.ingest.unknown_type")
        logger.warning("unknown_signal_types upsert failed: %s", e)
