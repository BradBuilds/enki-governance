"""
LDU Lineage — stamps every artifact with the GDU state at admission time.

The LDU (Local Data Unit) layer is the audit trail that lets us follow GDU
updates over time. Every entity, place, topic, knowledge_node, extraction
pass, chronicle — anything written to the database that depends on the
canonical vocabulary — must carry a lineage stamp recording WHICH version
of the GDU was the law when it was admitted.

Why this matters: the spine is sealed (we never edit packs in place). When
we eventually publish active_set_v6 with envelopevocab v2, the old v1 nodes
are NOT rewritten. They remain queryable forever via their lineage stamps.
You can ask "show me everything admitted under active_set_v5" and get a
clean answer. You can also walk back from any node and re-derive what its
envelope SHOULD look like under a newer pack version.

The stamp is small and cheap. It is a frozen snapshot:

    {
        "active_set_version": "5.0.0",
        "pack_versions": {
            "envelopevocab": "0.1.0",
            "nodevocab": "0.1.0",
            ...
        },
        "gdu_canonical": True,
        "stamped_at": "2026-04-10T22:30:00Z",
        "ldu_t_id": 8473829   # optional: pointer to raw truth row
    }

Usage from anywhere in the codebase:

    from core.ldu.lineage import get_lineage_stamp

    stamp = get_lineage_stamp(ldu_t_id=signal_rid)
    # ... insert with stamp.values into the lineage columns

The stamp is computed from the live GDU context — never hardcoded — so if
governance is degraded, the stamp will reflect that (gdu_canonical=False,
active_set_version=None) and the row is honestly marked as admitted under
non-canonical conditions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from core.gdu.signal_vocabulary import is_canonical, get_verification

log = logging.getLogger("enki.ldu.lineage")


# We refresh the cached pack version map at most once per process startup
# unless explicitly invalidated. Pack versions don't change while the
# process is running — the spine refuses to hot-swap.
_CACHED_PACK_VERSIONS: Optional[dict[str, str]] = None
_CACHED_ACTIVE_SET: Optional[str] = None


def _load_pack_versions_from_active_set() -> tuple[Optional[str], dict[str, str]]:
    """Read active set version + per-pack versions from the GDU context.

    Returns (active_set_version, {pack_id: version}). If GDU is degraded,
    returns (None, {}) so callers can stamp gdu_canonical=False.
    """
    try:
        from core.gdu.bootstrap import get_gdu
        gdu = get_gdu()
        active_set = gdu.active_set
        versions = {p.pack_id: p.version for p in active_set.packs}
        return active_set.version, versions
    except Exception as e:
        log.warning("Failed to load pack versions from GDU context: %s", e)
        return None, {}


def get_active_set_version() -> Optional[str]:
    """Return the currently admitted active set version (e.g. '5.0.0').

    Returns None if GDU is degraded or unreachable.
    """
    global _CACHED_ACTIVE_SET
    if _CACHED_ACTIVE_SET is None:
        _CACHED_ACTIVE_SET, _ = _load_pack_versions_from_active_set()
    return _CACHED_ACTIVE_SET


def get_pack_versions() -> dict[str, str]:
    """Return a snapshot of {pack_id: version} for every admitted pack.

    Returns empty dict if GDU is degraded.
    """
    global _CACHED_PACK_VERSIONS
    if _CACHED_PACK_VERSIONS is None:
        _, _CACHED_PACK_VERSIONS = _load_pack_versions_from_active_set()
    return dict(_CACHED_PACK_VERSIONS)  # defensive copy


def get_lineage_stamp(ldu_t_id: Optional[int] = None) -> dict:
    """Produce the lineage stamp dict that every new artifact should carry.

    Args:
        ldu_t_id: Optional pointer to the raw-truth (LDU-T) row this artifact
                  was derived from. Pass the signal_rid for things derived
                  from a signal; leave None for things derived from extraction
                  passes that don't have a single source signal.

    Returns:
        dict with keys: active_set_version, pack_versions, gdu_canonical,
        stamped_at, ldu_t_id

    The dict is JSON-serializable so callers can pass it directly into
    JSONB columns or split it across explicit lineage columns.
    """
    return {
        "active_set_version": get_active_set_version(),
        "pack_versions": get_pack_versions(),
        "gdu_canonical": is_canonical(),
        "stamped_at": datetime.now(timezone.utc).isoformat(),
        "ldu_t_id": ldu_t_id,
    }


def lineage_columns(ldu_t_id: Optional[int] = None) -> dict:
    """Return the lineage stamp shaped for direct INSERT into a table that
    has the standard lineage columns:

        admitted_under_active_set TEXT
        admitted_under_pack_versions JSONB
        gdu_canonical BOOLEAN
        ldu_t_id BIGINT

    Use like:
        cur.execute(
            "INSERT INTO entities (canonical_name, admitted_under_active_set, "
            "admitted_under_pack_versions, gdu_canonical, ldu_t_id) "
            "VALUES (%(name)s, %(active_set)s, %(pack_versions)s, %(canonical)s, %(ldu_t)s)",
            {"name": name, **lineage_columns(ldu_t_id=signal_rid)}
        )
    """
    import json as _json
    stamp = get_lineage_stamp(ldu_t_id=ldu_t_id)
    return {
        "active_set": stamp["active_set_version"],
        "pack_versions": _json.dumps(stamp["pack_versions"]),
        "canonical": stamp["gdu_canonical"],
        "ldu_t": stamp["ldu_t_id"],
    }


def reset_cache() -> None:
    """Invalidate the cached pack versions. For tests only — production
    code should never need this because the spine refuses to hot-swap
    pack versions while the process is running."""
    global _CACHED_PACK_VERSIONS, _CACHED_ACTIVE_SET
    _CACHED_PACK_VERSIONS = None
    _CACHED_ACTIVE_SET = None
