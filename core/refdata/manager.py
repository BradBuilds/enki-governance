"""
Refdata Manager — orchestrates local-cache sync for public datasets.

Reads the manifest at libraries/refdatapack/definitions/refdatapack.datasets.json
and provides:

  - list_packs()              → list available + status
  - is_fresh(pack_id)         → cheap check for source handlers
  - sync(pack_id)             → download + parse + index (one pack)
  - sync_all_due()            → batch sync of all packs past their cadence
  - get_pack_record(pack_id)  → registry row from refdata_packs

Each pack has its own parser at core.refdata.packs.<pack_id>. The parser
exposes a single `run(pack_def, sync_run_id)` function that returns a dict
of {records_indexed, bytes_downloaded, sha256, version}. The manager handles
the lifecycle (DB record, status updates, error capture).

Operator-facing flow:
  POST /refdata/sync/{pack_id}   → triggers a sync (background)
  GET  /refdata                   → status per pack
  GET  /refdata/{pack_id}         → detail + recent run history

Source-handler flow (the local-first hook):
  if refdata.manager.is_fresh("ofac_sdn"):
      hits = refdata.packs.ofac.lookup(name)
      ... build findings from local rows
  else:
      ... live API fallback
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.refdata.manager")

_MANIFEST_PATH = (
    Path(__file__).resolve().parents[2]
    / "libraries" / "refdatapack" / "definitions" / "refdatapack.datasets.json"
)

# On-disk staging for raw downloads. The pack parsers read from here and
# write parsed rows into the refdata.* tables. Keep raw dumps around so we
# can reproduce or audit an ingestion later.
_REFDATA_HOME = Path("/opt/enki/refdata")  # overridable via ENKI_REFDATA_HOME

_CADENCE_TO_SECONDS = {
    "daily":   24 * 3600,
    "weekly":  7 * 24 * 3600,
    "monthly": 30 * 24 * 3600,
    "quarterly": 90 * 24 * 3600,
    "yearly":  365 * 24 * 3600,
    "static":  10 * 365 * 24 * 3600,  # effectively never expires
}

_manifest_cache: Optional[dict] = None


def get_refdata_home() -> Path:
    """Resolve the on-disk refdata directory, honoring ENKI_REFDATA_HOME."""
    import os
    override = os.environ.get("ENKI_REFDATA_HOME", "").strip()
    home = Path(override) if override else _REFDATA_HOME
    home.mkdir(parents=True, exist_ok=True)
    return home


def load_manifest(force: bool = False) -> dict:
    """Read the refdatapack manifest. Cached after first call."""
    global _manifest_cache
    if _manifest_cache is not None and not force:
        return _manifest_cache
    try:
        with _MANIFEST_PATH.open("r", encoding="utf-8") as f:
            _manifest_cache = json.load(f)
    except Exception as e:
        log.warning("Failed to load refdatapack manifest: %s", e)
        _manifest_cache = {"datasets": []}
    return _manifest_cache


def reset_manifest_cache() -> None:
    global _manifest_cache
    _manifest_cache = None


def get_pack_def(pack_id: str) -> Optional[dict]:
    """Return the manifest entry for `pack_id` or None."""
    for d in load_manifest().get("datasets", []):
        if d.get("pack_id") == pack_id:
            return d
    return None


# ─── Registry access ────────────────────────────────────────────────────────

def ensure_registry_row(pack_def: dict) -> None:
    """Upsert the manifest entry into refdata_packs so the operator can see
    every known pack even before its first sync."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO refdata_packs
                    (pack_id, name, source_url, license, trust_class,
                     refresh_cadence, enabled, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)
                ON CONFLICT (pack_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    source_url = EXCLUDED.source_url,
                    license = EXCLUDED.license,
                    trust_class = EXCLUDED.trust_class,
                    refresh_cadence = EXCLUDED.refresh_cadence,
                    updated_at = NOW()
                """,
                (
                    pack_def["pack_id"],
                    pack_def["name"],
                    pack_def.get("source_urls", {}).get("primary", ""),
                    pack_def.get("license"),
                    pack_def.get("trust_class", "mentioned"),
                    pack_def.get("refresh_cadence"),
                    json.dumps({
                        "size_estimate_mb": pack_def.get("size_estimate_mb"),
                        "record_estimate": pack_def.get("record_estimate"),
                        "covers_entity_types": pack_def.get("covers_entity_types", []),
                        "served_sources": pack_def.get("served_sources", []),
                        "operator_opt_in_required": pack_def.get("operator_opt_in_required", False),
                        "notes": pack_def.get("notes", ""),
                    }),
                ),
            )
        conn.commit()


def ensure_all_registry_rows() -> int:
    """Sync the manifest into refdata_packs. Idempotent. Returns row count."""
    manifest = load_manifest()
    count = 0
    for d in manifest.get("datasets", []):
        ensure_registry_row(d)
        count += 1
    return count


def get_pack_record(pack_id: str) -> Optional[dict]:
    """Return the registry row for `pack_id`."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pack_id, name, version, source_url, license, trust_class,
                       refresh_cadence, on_disk_path, on_disk_bytes, record_count,
                       sha256, last_synced_at, last_sync_status, last_sync_error,
                       enabled, metadata, created_at, updated_at
                FROM refdata_packs WHERE pack_id = %s
                """,
                (pack_id,),
            )
            r = cur.fetchone()
            if not r:
                return None
            return {
                "pack_id": r[0], "name": r[1], "version": r[2],
                "source_url": r[3], "license": r[4], "trust_class": r[5],
                "refresh_cadence": r[6], "on_disk_path": r[7],
                "on_disk_bytes": r[8], "record_count": r[9],
                "sha256": r[10],
                "last_synced_at": r[11].isoformat() if r[11] else None,
                "last_sync_status": r[12], "last_sync_error": r[13],
                "enabled": r[14], "metadata": r[15],
                "created_at": r[16].isoformat() if r[16] else None,
                "updated_at": r[17].isoformat() if r[17] else None,
            }


def list_packs() -> list[dict]:
    """List every pack with current registry state. Used by /refdata GET."""
    ensure_all_registry_rows()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pack_id, name, version, trust_class, refresh_cadence,
                       record_count, on_disk_bytes, last_synced_at,
                       last_sync_status, last_sync_error, enabled, metadata
                FROM refdata_packs ORDER BY pack_id
                """
            )
            return [
                {
                    "pack_id": r[0], "name": r[1], "version": r[2],
                    "trust_class": r[3], "refresh_cadence": r[4],
                    "record_count": r[5], "on_disk_bytes": r[6],
                    "last_synced_at": r[7].isoformat() if r[7] else None,
                    "last_sync_status": r[8], "last_sync_error": r[9],
                    "enabled": r[10], "metadata": r[11],
                    "fresh": _is_fresh_from_row(r[4], r[7]),
                }
                for r in cur.fetchall()
            ]


def _is_fresh_from_row(cadence: Optional[str], last_synced) -> bool:
    if not last_synced:
        return False
    if not cadence:
        return False
    window = _CADENCE_TO_SECONDS.get(cadence, 24 * 3600)
    age = time.time() - last_synced.timestamp()
    return age < window


def is_fresh(pack_id: str) -> bool:
    """Cheap freshness check for source handlers. Returns True iff the pack
    has been synced and is within its cadence window AND is enabled.

    A handler should call this before doing a local-first lookup; if False,
    it falls through to the live API."""
    record = get_pack_record(pack_id)
    if not record or not record.get("enabled"):
        return False
    if record.get("last_sync_status") != "ok":
        return False
    last = record.get("last_synced_at")
    if not last:
        return False
    from datetime import datetime
    try:
        last_dt = datetime.fromisoformat(last)
    except Exception:
        return False
    window = _CADENCE_TO_SECONDS.get(record.get("refresh_cadence") or "", 24 * 3600)
    age = time.time() - last_dt.timestamp()
    return age < window


# ─── Sync orchestration ─────────────────────────────────────────────────────

def sync(pack_id: str) -> dict:
    """Run a single pack's sync. Idempotent — safe to call concurrently with
    other packs (each parser owns its own tables). Captures the run in
    refdata_sync_runs for audit."""
    pack_def = get_pack_def(pack_id)
    if not pack_def:
        return {"ok": False, "error": f"unknown pack_id: {pack_id}"}

    ensure_registry_row(pack_def)

    # Open a run record
    started = time.time()
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO refdata_sync_runs (pack_id, status)
                VALUES (%s, 'in_progress') RETURNING run_id
                """,
                (pack_id,),
            )
            run_id = cur.fetchone()[0]
            cur.execute(
                """
                UPDATE refdata_packs
                SET last_sync_status = 'in_progress', updated_at = NOW()
                WHERE pack_id = %s
                """,
                (pack_id,),
            )
        conn.commit()

    log.info("refdata.sync starting pack=%s run_id=%d", pack_id, run_id)

    try:
        # Load the per-pack parser. Convention: core.refdata.packs.<pack_id>
        # must expose run(pack_def, refdata_home).
        module_path = pack_def.get("parser_module") or f"core.refdata.packs.{pack_id}"
        module = importlib.import_module(module_path)
        if not hasattr(module, "run"):
            raise RuntimeError(
                f"Parser module {module_path} missing run(pack_def, refdata_home)"
            )
        outcome = module.run(pack_def, get_refdata_home())

        duration_ms = int((time.time() - started) * 1000)

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE refdata_sync_runs
                    SET completed_at = NOW(), status = 'ok',
                        records_indexed = %s, bytes_downloaded = %s,
                        duration_ms = %s
                    WHERE run_id = %s
                    """,
                    (
                        outcome.get("records_indexed", 0),
                        outcome.get("bytes_downloaded", 0),
                        duration_ms,
                        run_id,
                    ),
                )
                cur.execute(
                    """
                    UPDATE refdata_packs
                    SET version = %s,
                        record_count = %s,
                        on_disk_path = %s,
                        on_disk_bytes = %s,
                        sha256 = %s,
                        last_synced_at = NOW(),
                        last_sync_status = 'ok',
                        last_sync_error = NULL,
                        updated_at = NOW()
                    WHERE pack_id = %s
                    """,
                    (
                        outcome.get("version", ""),
                        outcome.get("records_indexed", 0),
                        outcome.get("on_disk_path", ""),
                        outcome.get("bytes_downloaded", 0),
                        outcome.get("sha256", ""),
                        pack_id,
                    ),
                )
            conn.commit()

        log.info(
            "refdata.sync complete pack=%s records=%d duration_ms=%d",
            pack_id, outcome.get("records_indexed", 0), duration_ms,
        )
        return {"ok": True, "pack_id": pack_id, "run_id": run_id, **outcome}

    except Exception as e:
        duration_ms = int((time.time() - started) * 1000)
        err = f"{type(e).__name__}: {e}"
        log.exception("refdata.sync FAILED pack=%s: %s", pack_id, err)
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE refdata_sync_runs
                        SET completed_at = NOW(), status = 'failed',
                            error = %s, duration_ms = %s
                        WHERE run_id = %s
                        """,
                        (err, duration_ms, run_id),
                    )
                    cur.execute(
                        """
                        UPDATE refdata_packs
                        SET last_sync_status = 'failed',
                            last_sync_error = %s,
                            updated_at = NOW()
                        WHERE pack_id = %s
                        """,
                        (err, pack_id),
                    )
                conn.commit()
        except Exception:
            log.exception("refdata.sync failure-bookkeeping ALSO failed")
        return {"ok": False, "pack_id": pack_id, "run_id": run_id, "error": err}


def sync_all_due() -> list[dict]:
    """Iterate all enabled packs, sync any that are past their cadence window.
    Called from the refdata sync worker on its schedule."""
    results = []
    for pack in list_packs():
        if not pack.get("enabled"):
            continue
        if pack.get("fresh"):
            continue
        results.append(sync(pack["pack_id"]))
    return results


# ─── Convenience helpers used by source handlers ────────────────────────────

def fetch_bytes(url: str, timeout: int = 60) -> tuple[bytes, str]:
    """Download a URL, return (body_bytes, sha256_hex). Used by pack parsers
    to keep download logic consistent. Caller is responsible for writing the
    bytes to disk and parsing."""
    import urllib.request
    import ssl
    ctx = ssl.create_default_context()
    req = urllib.request.Request(
        url, headers={"User-Agent": "Enki-Refdata/0.1 research@example.com"},
    )
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        body = resp.read()
    sha = hashlib.sha256(body).hexdigest()
    return body, sha
