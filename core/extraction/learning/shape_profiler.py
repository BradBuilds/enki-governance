"""
Shape profiler — record the structural shape of incoming payloads for
signal types that have no registered worker.

For each observed signal, the profiler walks the payload JSON tree and
records:
    - every field path (e.g. 'payload.properties.mag')
    - the observed value types (str/int/float/bool/list/dict/null)
    - a small rotating buffer of sample values per path
    - counts per path

Profiles are accumulated in the learned_mappings table, one row per
signal_type (the row's mapping_version column lets us version profiles
over time — each new active_set bumps version and starts fresh so
governance rolls are clean).

The profiler is resilient:
    - never raises — failures are logged and swallowed (the extraction
      runner must never break because learning failed)
    - bounds every list/dict it walks (MAX_DEPTH, MAX_FIELDS_PER_PAYLOAD,
      MAX_SAMPLES_PER_FIELD) to keep profiles small
    - redacts long string values (> 200 chars) to avoid storing PII or
      blowing up the DB
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from core.ldu.lineage import lineage_columns

log = logging.getLogger("enki.extraction.learning.profiler")

MAX_DEPTH = 6
MAX_FIELDS_PER_PAYLOAD = 200
MAX_SAMPLES_PER_FIELD = 5
MAX_STRING_LEN = 200


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return "other"


def _redact(v: Any) -> Any:
    """Cap string samples at MAX_STRING_LEN so we don't store PII/huge
    blobs. Non-strings pass through."""
    if isinstance(v, str):
        return v if len(v) <= MAX_STRING_LEN else v[:MAX_STRING_LEN] + "…"
    if isinstance(v, (list, dict)):
        # don't store nested structures as samples
        return f"<{_type_name(v)} len={len(v)}>"
    return v


def _walk(obj: Any, prefix: str, out: dict, depth: int, field_budget: list) -> None:
    if depth > MAX_DEPTH:
        return
    if field_budget[0] <= 0:
        return

    if isinstance(obj, dict):
        for k, v in obj.items():
            if field_budget[0] <= 0:
                return
            path = f"{prefix}.{k}" if prefix else k
            _walk(v, path, out, depth + 1, field_budget)
    elif isinstance(obj, list):
        # For lists, record the aggregate type and walk the first element
        # as a representative shape, not every element.
        path = f"{prefix}[]"
        entry = out.setdefault(path, {"types": {}, "samples": [], "count": 0})
        entry["count"] += 1
        entry["types"]["list"] = entry["types"].get("list", 0) + 1
        field_budget[0] -= 1
        if obj:
            _walk(obj[0], path, out, depth + 1, field_budget)
    else:
        entry = out.setdefault(prefix, {"types": {}, "samples": [], "count": 0})
        t = _type_name(obj)
        entry["types"][t] = entry["types"].get(t, 0) + 1
        entry["count"] += 1
        field_budget[0] -= 1
        if len(entry["samples"]) < MAX_SAMPLES_PER_FIELD:
            entry["samples"].append(_redact(obj))


def profile_payload(signal: dict) -> dict:
    """Walk a signal payload and return its shape profile. Pure function
    — no DB, no side effects."""
    out: dict[str, dict] = {}
    field_budget = [MAX_FIELDS_PER_PAYLOAD]
    try:
        _walk(signal.get("payload") or {}, "payload", out, 0, field_budget)
    except Exception as e:
        log.warning("profile_payload failed: %s", e)
    return out


def _merge_profile(existing: dict, new_fields: dict) -> tuple[dict, int]:
    """Merge a new observation into an existing profile. Returns the
    merged profile and the number of new field paths added."""
    added = 0
    for path, entry in new_fields.items():
        if path not in existing:
            existing[path] = {"types": {}, "samples": [], "count": 0}
            added += 1
        tgt = existing[path]
        for t, n in entry["types"].items():
            tgt["types"][t] = tgt["types"].get(t, 0) + n
        tgt["count"] += entry["count"]
        # Append samples up to cap
        for s in entry["samples"]:
            if len(tgt["samples"]) < MAX_SAMPLES_PER_FIELD and s not in tgt["samples"]:
                tgt["samples"].append(s)
    return existing, added


def observe(signal: dict, conn=None) -> None:
    """Record one payload observation against its signal_type.

    No-op if conn is None (e.g. offline corpus runs). Never raises —
    failures are logged so extraction never breaks because of learning.
    """
    if conn is None:
        return
    signal_type = signal.get("type") or signal.get("signal_type")
    if not signal_type:
        return

    try:
        new_fields = profile_payload(signal)
        if not new_fields:
            return

        lin = lineage_columns(ldu_t_id=signal.get("signal_rid"))

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mapping_id, shape_profile, sample_count
                  FROM learned_mappings
                 WHERE signal_type = %s AND status = 'observing'
                 ORDER BY mapping_version DESC
                 LIMIT 1
                """,
                (signal_type,),
            )
            row = cur.fetchone()

            if row is None:
                cur.execute(
                    """
                    INSERT INTO learned_mappings (
                        signal_type, mapping_version, status,
                        shape_profile, sample_count,
                        admitted_under_active_set,
                        admitted_under_pack_versions,
                        gdu_canonical
                    ) VALUES (
                        %(signal_type)s, 1, 'observing',
                        %(profile)s::jsonb, 1,
                        %(active_set)s, %(pack_versions)s::jsonb, %(canonical)s
                    )
                    """,
                    {
                        "signal_type": signal_type,
                        "profile": json.dumps(new_fields),
                        **lin,
                    },
                )
            else:
                mapping_id, existing_profile, sample_count = row
                merged, _added = _merge_profile(
                    existing_profile or {}, new_fields
                )
                cur.execute(
                    """
                    UPDATE learned_mappings
                       SET shape_profile = %s::jsonb,
                           sample_count = %s,
                           last_observed_at = NOW(),
                           updated_at = NOW()
                     WHERE mapping_id = %s
                    """,
                    (json.dumps(merged), sample_count + 1, mapping_id),
                )
        conn.commit()
    except Exception as e:
        log.warning("observe() failed for %s: %s", signal_type, e)
        try:
            conn.rollback()
        except Exception:
            pass


def get_profile(signal_type: str, conn) -> Optional[dict]:
    """Read the current observing profile for a signal_type."""
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT shape_profile, sample_count, status, mapping_version
                  FROM learned_mappings
                 WHERE signal_type = %s
                 ORDER BY mapping_version DESC
                 LIMIT 1
                """,
                (signal_type,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            profile, count, status, version = row
            return {
                "signal_type": signal_type,
                "shape_profile": profile,
                "sample_count": count,
                "status": status,
                "mapping_version": version,
            }
    except Exception as e:
        log.warning("get_profile(%s) failed: %s", signal_type, e)
        return None
