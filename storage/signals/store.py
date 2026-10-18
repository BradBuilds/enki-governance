"""
Signal storage operations — insert and query for ldu_t_signals, signals, ldu_p_entries.

All insert functions use parameterized queries (no SQL injection).
All queries filter by tenant_id (FD-4 tenant isolation).
Append-only tables are protected by database triggers (schema_v2.sql).
"""

import json
import hashlib
from datetime import datetime, timezone
from typing import Optional

from canonicaljson import encode_canonical_json

from storage.postgres.db import get_connection
from storage.signals.models import LduTSignal, Signal, LduPEntry, Observable


# ─── LDU-T: Raw Truth Storage ─────────────────────────────────────────────────

def insert_ldu_t(entry: LduTSignal, dsn: Optional[str] = None) -> int:
    """Store raw signal bytes in LDU-T. Returns database rid."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ldu_t_signals
                    (ldu_t_id, tenant_id, node_id, device_id, source_type,
                     raw_bytes, raw_sha256, raw_byte_length, content_type,
                     format_detected, detection_confidence)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, raw_sha256) DO NOTHING
                RETURNING rid
            """, (
                entry.ldu_t_id, entry.tenant_id, entry.node_id,
                entry.device_id, entry.source_type,
                entry.raw_bytes, entry.raw_sha256, entry.raw_byte_length,
                entry.content_type, entry.format_detected,
                entry.detection_confidence,
            ))
            row = cur.fetchone()
            conn.commit()
            return row[0] if row else -1


def get_ldu_t(ldu_t_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    """Retrieve an LDU-T entry by ID."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ldu_t_id, tenant_id, node_id, device_id, source_type,
                       raw_sha256, raw_byte_length, content_type,
                       format_detected, detection_confidence, ingestion_timestamp
                FROM ldu_t_signals
                WHERE ldu_t_id = %s AND tenant_id = %s
            """, (ldu_t_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "ldu_t_id": row[0], "tenant_id": row[1], "node_id": row[2],
                "device_id": row[3], "source_type": row[4],
                "raw_sha256": row[5], "raw_byte_length": row[6],
                "content_type": row[7], "format_detected": row[8],
                "detection_confidence": row[9],
                "ingestion_timestamp": row[10].isoformat() if row[10] else None,
            }


# ─── Signals: Normalized (LDU-W) ──────────────────────────────────────────────

def compute_signal_cid(signal: Signal) -> str:
    """Compute content hash for a signal. Excludes storage-assigned fields."""
    hashable = {
        "signal_type": signal.signal_type,
        "source_domain": signal.source_domain,
        "device_id": signal.device_id,
        "timestamp": signal.timestamp.isoformat(),
        "payload": signal.payload,
        "ldu_t_id": signal.ldu_t_id,
        "node_id": signal.node_id,
    }
    canonical = encode_canonical_json(hashable)
    return hashlib.sha256(canonical).hexdigest()


def insert_signal(signal: Signal, dsn: Optional[str] = None) -> int:
    """Store a normalized signal. Returns database rid.

    Cross-source dedup (Phase 2 of the news-collapse plan): if the
    signal's payload carries an article URL, we canonicalize it (strip
    tracking params / AMP / mobile / fragment / scheme case) and stamp
    a 16-char hash on the row. After insert, if an EARLIER signal in
    the same tenant has the same url_hash, we write a signal_aliases
    row pointing this new signal at that earlier "canonical" one.
    No hard rejection — every signal still gets its own row so source
    attribution is preserved.
    """
    # Compute canonical-URL hash before INSERT. Imported here (not at module
    # top) to avoid a circular import: core.extraction sometimes imports
    # storage.signals.* during its own startup.
    from core.extraction.url_canonical import url_hash as _url_hash, extract_signal_url
    article_url = extract_signal_url(signal.payload)
    u_hash = _url_hash(article_url) if article_url else None

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Extract facets from metadata for the dedicated facets column
            facets_json = json.dumps(signal.metadata.get("facets", {}))

            cur.execute("""
                INSERT INTO signals
                    (signal_id, tenant_id, node_id, device_id, identity_id,
                     signal_type, source_domain, severity, confidence,
                     timestamp, server_time,
                     latitude, longitude, altitude, accuracy,
                     payload, observables, metadata, facets,
                     ldu_t_id, ldu_p_id, cid,
                     origin_node_id, federation_id, route_path,
                     device_signature, url_hash)
                VALUES (%s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, NOW(),
                        %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s)
                RETURNING rid
            """, (
                signal.signal_id, signal.tenant_id, signal.node_id,
                signal.device_id, signal.identity_id,
                signal.signal_type, signal.source_domain,
                signal.severity, signal.confidence,
                signal.timestamp,
                signal.latitude, signal.longitude,
                signal.altitude, signal.accuracy,
                json.dumps(signal.payload),
                json.dumps(signal.observables),
                json.dumps(signal.metadata),
                facets_json,
                signal.ldu_t_id, signal.ldu_p_id, signal.cid,
                signal.origin_node_id, signal.federation_id,
                signal.route_path,
                signal.device_signature, u_hash,
            ))
            rid = cur.fetchone()[0]

            # Cross-source alias detection. Look for the earliest
            # signal in this tenant with the same url_hash that's NOT
            # the row we just inserted; if one exists, declare this row
            # an alias of it. Best-effort — failures here don't block
            # signal ingestion.
            if u_hash:
                try:
                    cur.execute("""
                        SELECT rid FROM signals
                         WHERE tenant_id = %s AND url_hash = %s AND rid <> %s
                         ORDER BY rid ASC
                         LIMIT 1
                    """, (signal.tenant_id, u_hash, rid))
                    earlier = cur.fetchone()
                    if earlier:
                        cur.execute("""
                            INSERT INTO signal_aliases
                                (tenant_id, signal_rid, canonical_signal_rid,
                                 url_hash, matched_url, detected_by)
                            VALUES (%s, %s, %s, %s, %s, 'url_canonical_v1')
                            ON CONFLICT ON CONSTRAINT signal_aliases_unique_signal
                                DO NOTHING
                        """, (
                            signal.tenant_id, rid, earlier[0],
                            u_hash, article_url,
                        ))
                except Exception:
                    # Don't let alias bookkeeping fail the signal insert.
                    # If the table doesn't exist yet (migration not applied)
                    # or any other error, swallow and continue.
                    pass

            conn.commit()
            return rid


def get_signal(signal_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    """Retrieve a signal by ID."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT signal_id, tenant_id, node_id, device_id, identity_id,
                       signal_type, source_domain, severity, confidence,
                       timestamp, server_time,
                       latitude, longitude, altitude, accuracy,
                       payload, observables, metadata,
                       ldu_t_id, ldu_p_id, cid, rid,
                       origin_node_id, federation_id, route_path
                FROM signals
                WHERE signal_id = %s AND tenant_id = %s
            """, (signal_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "signal_id": row[0], "tenant_id": row[1], "node_id": row[2],
                "device_id": row[3], "identity_id": row[4],
                "signal_type": row[5], "source_domain": row[6],
                "severity": row[7], "confidence": row[8],
                "timestamp": row[9].isoformat(), "server_time": row[10].isoformat(),
                "latitude": row[11], "longitude": row[12],
                "altitude": row[13], "accuracy": row[14],
                "payload": row[15], "observables": row[16], "metadata": row[17],
                "ldu_t_id": row[18], "ldu_p_id": row[19],
                "cid": row[20], "rid": row[21],
                "origin_node_id": row[22], "federation_id": row[23],
                "route_path": row[24],
            }


def query_signals(
    tenant_id: str,
    signal_type: Optional[str] = None,
    device_id: Optional[str] = None,
    after: Optional[datetime] = None,
    before: Optional[datetime] = None,
    limit: int = 100,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Query signals with filters. Always scoped to tenant_id."""
    conditions = ["tenant_id = %s"]
    params: list = [tenant_id]

    if signal_type:
        conditions.append("signal_type = %s")
        params.append(signal_type)
    if device_id:
        conditions.append("device_id = %s")
        params.append(device_id)
    if after:
        conditions.append("timestamp >= %s")
        params.append(after)
    if before:
        conditions.append("timestamp <= %s")
        params.append(before)

    params.append(min(limit, 1000))  # hard cap at 1000 (no bulk export via API)

    where = " AND ".join(conditions)

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT signal_id, signal_type, source_domain, severity,
                       timestamp, device_id, identity_id,
                       latitude, longitude, cid, rid
                FROM signals
                WHERE {where}
                ORDER BY timestamp DESC
                LIMIT %s
            """, params)
            rows = cur.fetchall()
            return [
                {
                    "signal_id": r[0], "signal_type": r[1],
                    "source_domain": r[2], "severity": r[3],
                    "timestamp": r[4].isoformat(), "device_id": r[5],
                    "identity_id": r[6], "latitude": r[7],
                    "longitude": r[8], "cid": r[9], "rid": r[10],
                }
                for r in rows
            ]


# ─── LDU-P: Provisional Layer ─────────────────────────────────────────────────

def insert_ldu_p(entry: LduPEntry, dsn: Optional[str] = None) -> int:
    """Stage a provisional entry in LDU-P. Returns database rid."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO ldu_p_entries
                    (ldu_p_id, tenant_id, ldu_t_id,
                     format_detected, detection_confidence, parser_used,
                     extracted_structure, proposed_mapping,
                     proposed_signal_type, vocabulary_gaps, resolution)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING rid
            """, (
                entry.ldu_p_id, entry.tenant_id, entry.ldu_t_id,
                entry.format_detected, entry.detection_confidence,
                entry.parser_used,
                json.dumps(entry.extracted_structure),
                json.dumps(entry.proposed_mapping),
                entry.proposed_signal_type,
                entry.vocabulary_gaps,
                entry.resolution,
            ))
            rid = cur.fetchone()[0]
            conn.commit()
            return rid


def get_ldu_p(ldu_p_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[dict]:
    """Retrieve an LDU-P entry by ID."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ldu_p_id, tenant_id, ldu_t_id,
                       format_detected, detection_confidence, parser_used,
                       extracted_structure, proposed_mapping,
                       proposed_signal_type, vocabulary_gaps,
                       resolution, resolution_path, resolved_by, resolved_at,
                       promoted_signal_rid, created_at
                FROM ldu_p_entries
                WHERE ldu_p_id = %s AND tenant_id = %s
            """, (ldu_p_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "ldu_p_id": row[0], "tenant_id": row[1], "ldu_t_id": row[2],
                "format_detected": row[3], "detection_confidence": row[4],
                "parser_used": row[5], "extracted_structure": row[6],
                "proposed_mapping": row[7], "proposed_signal_type": row[8],
                "vocabulary_gaps": row[9], "resolution": row[10],
                "resolution_path": row[11], "resolved_by": row[12],
                "resolved_at": row[13].isoformat() if row[13] else None,
                "promoted_signal_rid": row[14],
                "created_at": row[15].isoformat(),
            }


def update_ldu_p(
    ldu_p_id: str,
    tenant_id: str,
    *,
    proposed_signal_type: Optional[str] = None,
    extracted_structure: Optional[dict] = None,
    proposed_mapping: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Edit a pending LDU-P entry before promotion. Returns True on update.

    Only rows in resolution='pending_review' can be edited.
    """
    import json as _json
    sets: list[str] = []
    params: list = []
    if proposed_signal_type is not None:
        sets.append("proposed_signal_type = %s")
        params.append(proposed_signal_type)
    if extracted_structure is not None:
        sets.append("extracted_structure = %s::jsonb")
        params.append(_json.dumps(extracted_structure))
    if proposed_mapping is not None:
        sets.append("proposed_mapping = %s::jsonb")
        params.append(_json.dumps(proposed_mapping))
    if not sets:
        return False
    params += [ldu_p_id, tenant_id]
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE ldu_p_entries
                   SET {', '.join(sets)}
                 WHERE ldu_p_id = %s AND tenant_id = %s
                   AND resolution = 'pending_review'
                """,
                params,
            )
            updated = cur.rowcount > 0
            conn.commit()
            return updated


def resolve_ldu_p(
    ldu_p_id: str,
    tenant_id: str,
    resolution: str,
    resolution_path: str,
    resolved_by: str,
    promoted_signal_rid: Optional[int] = None,
    dsn: Optional[str] = None,
) -> bool:
    """Resolve an LDU-P entry. Returns True if updated."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE ldu_p_entries
                SET resolution = %s,
                    resolution_path = %s,
                    resolved_by = %s,
                    resolved_at = NOW(),
                    promoted_signal_rid = %s
                WHERE ldu_p_id = %s AND tenant_id = %s
                  AND resolution = 'pending_review'
            """, (
                resolution, resolution_path, resolved_by,
                promoted_signal_rid, ldu_p_id, tenant_id,
            ))
            updated = cur.rowcount > 0
            conn.commit()
            return updated


def list_pending_ldu_p(
    tenant_id: str,
    limit: int = 100,
    dsn: Optional[str] = None,
) -> list[dict]:
    """List pending LDU-P entries for a tenant."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ldu_p_id, ldu_t_id, format_detected,
                       proposed_signal_type, vocabulary_gaps, created_at
                FROM ldu_p_entries
                WHERE tenant_id = %s AND resolution = 'pending_review'
                ORDER BY created_at ASC
                LIMIT %s
            """, (tenant_id, min(limit, 1000)))
            return [
                {
                    "ldu_p_id": r[0], "ldu_t_id": r[1],
                    "format_detected": r[2], "proposed_signal_type": r[3],
                    "vocabulary_gaps": r[4],
                    "created_at": r[5].isoformat(),
                }
                for r in cur.fetchall()
            ]


# ─── Observable Index ──────────────────────────────────────────────────────────

def insert_observables(
    tenant_id: str,
    signal_rid: int,
    observables: list[Observable],
    dsn: Optional[str] = None,
) -> int:
    """Insert extracted observables into the index. Returns count inserted."""
    if not observables:
        return 0
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            count = 0
            for obs in observables:
                cur.execute("""
                    INSERT INTO observable_index
                        (tenant_id, observable_type, observable_value,
                         normalized_value, signal_rid, source_field)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, observable_type, normalized_value, signal_rid)
                    DO NOTHING
                """, (
                    tenant_id, obs.observable_type, obs.observable_value,
                    obs.normalized_value, signal_rid, obs.source_field,
                ))
                count += cur.rowcount
            conn.commit()
            return count


def search_observables(
    tenant_id: str,
    observable_type: str,
    normalized_value: str,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Find all signals containing a specific observable."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT oi.observable_type, oi.observable_value,
                       oi.signal_rid, oi.source_field, oi.extracted_at,
                       s.signal_id, s.signal_type, s.timestamp
                FROM observable_index oi
                JOIN signals s ON s.rid = oi.signal_rid
                WHERE oi.tenant_id = %s
                  AND oi.observable_type = %s
                  AND oi.normalized_value = %s
                ORDER BY s.timestamp DESC
                LIMIT 100
            """, (tenant_id, observable_type, normalized_value))
            return [
                {
                    "observable_type": r[0], "observable_value": r[1],
                    "signal_rid": r[2], "source_field": r[3],
                    "extracted_at": r[4].isoformat(),
                    "signal_id": r[5], "signal_type": r[6],
                    "signal_timestamp": r[7].isoformat(),
                }
                for r in cur.fetchall()
            ]
