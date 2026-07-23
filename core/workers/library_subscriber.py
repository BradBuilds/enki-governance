"""Library subscriber — pulls mirrored content from upstream nodes.

Counterpart to the library_work_assigner / yield_accountant on the
publisher side. This worker runs on subscribing nodes:

  - Walks `library_mirrors` where status='active' (one row per outgoing
    subscription this node has)
  - For each, fetches the upstream's signed library manifest from
    `${upstream_url}/libraries/${upstream_library_id}/manifest.enki`
  - Diffs against local documents.content_sha256
  - Pulls missing content via `${upstream_url}/p2p/content/{sha256}`
  - Verifies sha256 on receipt (content addressing — any mismatch
    means corruption or hostile upstream; bytes are discarded)
  - Writes the .enki blob to local corpus storage
  - Inserts into local `documents` table + binds to the local library
    via `library_members`

Sister worker `extraction_subscriber.py` (Batch C) takes over after
documents land — checks the hub for cached extraction envelopes before
launching a local LLM run.

Run modes — same shape as federation_subscriber.py:
  start_worker(tenant_id) / stop_worker / get_status / run_tick

Mirror creation is a separate API call (POST /libraries/mirror on the
subscriber side); this worker only consumes pre-existing library_mirrors
rows. Operator-driven addition of a mirror, not automatic discovery.
"""
from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("enki.workers.library_subscriber")

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP_EVENT = threading.Event()
_STATUS: dict = {
    "running": False,
    "phase": "idle",
    "last_run": None,
    "ticks": 0,
    "mirrors_processed": 0,
    "documents_pulled": 0,
    "documents_failed": 0,
    "bytes_pulled": 0,
    "errors": 0,
    "last_error": None,
}

# Walk active mirrors every 15 min. When something was actually pulled,
# stay on the 15-min tick; if nothing changed, back off to 30 min.
_TICK_INTERVAL_S = 900
_IDLE_BACKOFF_S = 1800

# Per-document HTTP timeout — these can be large PDFs.
_DOWNLOAD_TIMEOUT_S = 120

# How many missing documents to attempt per mirror per tick. Limits
# bandwidth + DB pressure when a mirror is freshly created and has
# hundreds of new docs to pull.
_MAX_DOCS_PER_MIRROR_PER_TICK = 50


def get_status() -> dict:
    return dict(_STATUS)


def start_worker(tenant_id: str = "default") -> dict:
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP_EVENT.clear()
    _WORKER_THREAD = threading.Thread(
        target=_loop, args=(tenant_id,),
        name="library-subscriber", daemon=True,
    )
    _WORKER_THREAD.start()
    _STATUS["running"] = True
    log.info("Library subscriber started")
    return {"status": "started"}


def stop_worker() -> dict:
    _STOP_EVENT.set()
    if _WORKER_THREAD:
        _WORKER_THREAD.join(timeout=10)
    _STATUS["running"] = False
    return {"status": "stopped"}


def _loop(tenant_id: str) -> None:
    backoff = _TICK_INTERVAL_S
    while not _STOP_EVENT.is_set():
        try:
            res = run_tick(tenant_id=tenant_id)
            _STATUS["ticks"] += 1
            _STATUS["last_run"] = datetime.now(timezone.utc).isoformat()
            _STATUS["mirrors_processed"] += res.get("mirrors_processed", 0)
            _STATUS["documents_pulled"] += res.get("documents_pulled", 0)
            _STATUS["documents_failed"] += res.get("documents_failed", 0)
            _STATUS["bytes_pulled"] += res.get("bytes_pulled", 0)
            _STATUS["phase"] = (
                f"tick: {res.get('mirrors_processed',0)} mirrors, "
                f"{res.get('documents_pulled',0)} pulled, "
                f"{res.get('documents_failed',0)} failed"
            )
            backoff = (
                _TICK_INTERVAL_S if res.get("documents_pulled", 0) > 0
                else _IDLE_BACKOFF_S
            )
        except Exception as e:
            log.warning("library_subscriber tick failed: %s", e)
            _STATUS["errors"] += 1
            _STATUS["last_error"] = str(e)[:300]
            _STATUS["phase"] = f"error: {str(e)[:60]}"
            backoff = _IDLE_BACKOFF_S

        for _ in range(int(backoff)):
            if _STOP_EVENT.is_set():
                break
            time.sleep(1)
    _STATUS["running"] = False


def _discover_new_hosts_from_hub(tenant_id: str) -> int:
    """Federation Stage 3 discovery: ask the hub catalog which OTHER peers
    host libraries that we're already subscribed to. For each newly-seen
    host, insert a library_mirrors row so the next tick picks it up.

    Auto-redundancy: if node-A publishes library X and we subscribe to X via
    node-A, when a third peer also begins publishing X, we automatically
    start mirroring from them too. No operator action needed.

    Read-only against the hub. Insert-only against library_mirrors.
    Gated on ENKI_HUB_URL — single-node deployments skip this entirely.

    Returns the number of new mirrors inserted."""
    import os
    hub_url = os.environ.get("ENKI_HUB_URL")
    if not hub_url:
        return 0

    from storage.postgres.db import get_connection
    try:
        from storage.node.identity import load_node_id
        own_node_id = load_node_id() or os.environ.get("ENKI_NODE_ID", "")
    except Exception:
        own_node_id = os.environ.get("ENKI_NODE_ID", "")

    # Map federation_id -> local_library_id for libraries we already mirror
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT l.federation_id::text, lm.local_library_id
                  FROM library_mirrors lm
                  JOIN libraries l ON l.library_id = lm.local_library_id
                 WHERE lm.status = 'active'
                   AND l.federation_id IS NOT NULL
                   AND l.tenant_id = %s
                """,
                (tenant_id,),
            )
            fid_to_local = {row[0]: row[1] for row in cur.fetchall()}
            cur.execute(
                "SELECT upstream_node_id, local_library_id FROM library_mirrors WHERE status = 'active'"
            )
            existing_mirrors = {(row[0], row[1]) for row in cur.fetchall()}

    if not fid_to_local:
        return 0

    try:
        import requests as _rq
        r = _rq.get(f"{hub_url}/hub/catalog", params={"limit": 500}, timeout=10)
        if not r.ok:
            log.debug("hub catalog query returned %s; skipping discovery", r.status_code)
            return 0
        entries = (r.json() or {}).get("entries") or []
    except Exception as e:
        log.debug("hub catalog query failed (non-fatal): %s", e)
        return 0

    inserted = 0
    with get_connection() as conn:
        with conn.cursor() as cur:
            for entry in entries:
                fid = entry.get("federation_id")
                if fid not in fid_to_local:
                    continue
                local_lib = fid_to_local[fid]
                for host in entry.get("hosts") or []:
                    host_node = host.get("node_id")
                    manifest_url = host.get("manifest_url")
                    if not host_node or not manifest_url:
                        continue
                    if host_node == own_node_id:
                        continue
                    if (host_node, local_lib) in existing_mirrors:
                        continue
                    try:
                        # Derive upstream library_id from manifest_url:
                        # pattern is .../api/libraries/{id}/manifest.enki...
                        ulib_id = 0
                        import re as _re
                        m = _re.search(r"/libraries/(\d+)/", manifest_url)
                        if m:
                            ulib_id = int(m.group(1))
                        cur.execute(
                            """
                            INSERT INTO library_mirrors
                                (local_library_id, upstream_node_id,
                                 upstream_library_id, upstream_url, status)
                            VALUES (%s, %s, %s, %s, 'active')
                            ON CONFLICT DO NOTHING
                            """,
                            (local_lib, host_node, ulib_id, manifest_url),
                        )
                        if cur.rowcount > 0:
                            inserted += 1
                            log.info(
                                "Stage 3 discovery: added mirror for federation_id=%s "
                                "via node=%s (manifest=%s)",
                                fid, host_node, manifest_url,
                            )
                    except Exception as e:
                        log.debug("auto-mirror insert failed (non-fatal): %s", e)
            if inserted > 0:
                conn.commit()
    return inserted


def run_tick(*, tenant_id: str = "default") -> dict:
    """One subscriber pass: walk all active mirrors, fetch manifests,
    pull missing content. Returns a stats dict for logging."""
    from storage.postgres.db import get_connection

    # Stage 3 discovery: find new producers of libraries we already
    # mirror. Adds library_mirrors rows; next tick will pull from them.
    try:
        new_mirrors = _discover_new_hosts_from_hub(tenant_id)
        if new_mirrors:
            log.info("Stage 3 discovery added %d new mirror(s) this tick", new_mirrors)
    except Exception as e:
        log.debug("stage 3 discovery failed (non-fatal): %s", e)

    totals = {
        "mirrors_processed": 0,
        "documents_pulled": 0,
        "documents_failed": 0,
        "bytes_pulled": 0,
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mirror_id, local_library_id, upstream_node_id,
                       upstream_library_id, upstream_url, manifest_hash,
                       last_synced_at
                  FROM library_mirrors
                 WHERE status = 'active'
                 ORDER BY last_synced_at ASC NULLS FIRST
                """
            )
            mirrors = cur.fetchall()

    for row in mirrors:
        if _STOP_EVENT.is_set():
            break
        (mirror_id, local_lib, upstream_node, upstream_lib,
         upstream_url, prev_hash, last_synced_at) = row
        try:
            r = _sync_mirror(
                mirror_id=mirror_id,
                local_library_id=local_lib,
                upstream_node_id=upstream_node,
                upstream_library_id=upstream_lib,
                upstream_url=upstream_url,
                prev_manifest_hash=prev_hash,
                last_synced_at=last_synced_at,
                tenant_id=tenant_id,
            )
            totals["mirrors_processed"] += 1
            totals["documents_pulled"] += r.get("pulled", 0)
            totals["documents_failed"] += r.get("failed", 0)
            totals["bytes_pulled"] += r.get("bytes", 0)
        except Exception:
            log.exception("mirror %s failed", mirror_id)
            totals["documents_failed"] += 1
            _record_mirror_error(mirror_id, _STATUS.get("last_error") or "unknown")

    return totals


def _sync_mirror(
    *,
    mirror_id: int,
    local_library_id: int,
    upstream_node_id: str,
    upstream_library_id: int,
    upstream_url: str,
    prev_manifest_hash: Optional[str],
    tenant_id: str,
    last_synced_at: Optional[Any] = None,
) -> dict:
    """Fetch the upstream manifest, diff against local content, pull
    missing docs. Returns {pulled, failed, bytes, manifest_unchanged}."""
    import httpx

    from core.enki.format import loads_enki
    from core.enki.ingest import enki_path_for_hash
    from storage.postgres.db import get_connection

    base = upstream_url.rstrip("/")
    # Request v2 manifest (Phase 5 federation bundle). Upstreams that
    # don't yet understand ?v=2 fall through to v1 — both shapes are
    # handled below. Phase 7: pass ?since=<last_synced_at> so upstream
    # returns only items changed since our last successful sync.
    qs = "v=2"
    if last_synced_at is not None:
        qs += f"&since={last_synced_at.isoformat()}"
    manifest_url = f"{base}/libraries/{upstream_library_id}/manifest.enki?{qs}"

    log.debug("syncing mirror %s from %s", mirror_id, manifest_url)

    # Peer JWT auth (matches extraction_subscriber pattern). The manifest
    # endpoint is unified_auth-gated; fetches without a JWT get 401.
    # Best-effort: if no JWT cached, we send no auth headers and let the
    # upstream decide (some peers may serve public libraries unauthenticated
    # in future phases).
    try:
        from core.federation.peer_identity import authorized_headers
        _auth_headers = authorized_headers()
    except Exception:
        _auth_headers = {}

    with httpx.Client(timeout=30, headers=_auth_headers) as client:
        resp = client.get(manifest_url)
        if resp.status_code == 304:
            # Upstream supports If-None-Match; manifest unchanged
            return {"pulled": 0, "failed": 0, "bytes": 0, "manifest_unchanged": True}
        if resp.status_code == 401:
            # Most common cause: no peer JWT cached locally. Log
            # actionable remediation instead of a generic error.
            has_auth = "authorization" in {k.lower() for k in _auth_headers}
            raise RuntimeError(
                f"manifest fetch returned 401 — peer auth missing or invalid. "
                f"{'JWT was attached but rejected — try `peer_identity refresh` or check trust_score' if has_auth else 'No JWT cached — run `systemctl start enki-peer-bootstrap.service`'}"
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"manifest fetch returned {resp.status_code} from {manifest_url} — "
                f"check upstream node reachability + library is_public flag"
            )
        manifest_bytes = resp.content
        content_type = resp.headers.get("content-type", "")

    # The manifest endpoint returns a JSON message (not CBOR) when the
    # upstream library has zero documents with content_sha256. Treat that
    # as a successful empty sync, not an error — the upstream just hasn't
    # populated hashes yet (typical for docs ingested before migration 094).
    if "json" in content_type or not manifest_bytes.startswith(b"\xa6") and not manifest_bytes.startswith(b"\xa7"):
        try:
            import json as _json
            empty = _json.loads(manifest_bytes)
            if empty.get("document_count") == 0:
                log.info(
                    "mirror %s: upstream library %s has 0 documents with content_sha256 "
                    "(message: %s) — nothing to pull",
                    mirror_id, upstream_library_id, empty.get("message", "")
                )
                _record_mirror_synced(mirror_id, "empty-manifest", pulled=0, failed=0, bytes_pulled=0)
                return {"pulled": 0, "failed": 0, "bytes": 0, "empty_upstream": True}
        except Exception:
            pass  # fall through to CBOR parse, which will raise a better error

    # Verify signature + content_hash
    manifest_obj = loads_enki(manifest_bytes, verify=True)
    # EnkiObject.content_hash format is "sha256:<64hex>" (71 chars). Our
    # library_mirrors.manifest_hash column is CHAR(64) so strip the prefix.
    manifest_hash = manifest_obj.content_hash
    if manifest_hash.startswith("sha256:"):
        manifest_hash = manifest_hash[7:]

    # Skip the rest if manifest hasn't changed since last successful sync
    if prev_manifest_hash and prev_manifest_hash == manifest_hash:
        _record_mirror_synced(mirror_id, manifest_hash, pulled=0, failed=0, bytes_pulled=0)
        return {"pulled": 0, "failed": 0, "bytes": 0, "manifest_unchanged": True}

    # Manifest content shape (from build_manifest in core/enki/format.py):
    # { library_id, library_canonical_name, manifest_version, document_count,
    #   total_size_bytes, documents: [{content_hash, source_title, source_url,
    #                                  size_bytes}, ...] }
    docs = manifest_obj.content.get("documents") or []

    # Diff against local — which content_sha256s do we NOT already have?
    upstream_hashes = []
    hash_to_meta = {}
    for d in docs:
        ch = d.get("content_hash", "")
        if ch.startswith("sha256:"):
            ch = ch[7:]
        if not ch or len(ch) != 64:
            continue
        upstream_hashes.append(ch)
        hash_to_meta[ch] = d

    if not upstream_hashes:
        _record_mirror_synced(mirror_id, manifest_hash, pulled=0, failed=0, bytes_pulled=0)
        return {"pulled": 0, "failed": 0, "bytes": 0}

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT content_sha256 FROM documents WHERE content_sha256 = ANY(%s)",
                (upstream_hashes,),
            )
            already_have = {r[0] for r in cur.fetchall()}

    missing = [h for h in upstream_hashes if h not in already_have]
    log.info(
        "mirror %s: manifest has %d docs, local has %d, fetching %d",
        mirror_id, len(upstream_hashes), len(already_have), len(missing),
    )

    # Cap per-tick fetches so first-time syncs don't blow out bandwidth
    fetch_now = missing[:_MAX_DOCS_PER_MIRROR_PER_TICK]
    pulled = 0
    failed = 0
    bytes_pulled = 0

    with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_S, headers=_auth_headers) as client:
        for sha in fetch_now:
            if _STOP_EVENT.is_set():
                break
            try:
                got = _pull_and_store_document(
                    client=client,
                    base_url=base,
                    content_sha256=sha,
                    upstream_meta=hash_to_meta[sha],
                    local_library_id=local_library_id,
                    tenant_id=tenant_id,
                )
                pulled += 1
                bytes_pulled += got
                log.debug("pulled %s (%d bytes)", sha[:8], got)
            except Exception as e:
                failed += 1
                log.warning("pull failed for %s: %s", sha[:16], e)

    # Phase 5: if upstream sent a v2 manifest, sync the bundle sections
    # (entities, relationships, federation_id) AFTER docs are pulled.
    # Extractions[] is informational — extraction_subscriber's tick will
    # naturally pull each envelope via /fleet/extractions/find.
    v2_summary = None
    if int(manifest_obj.content.get("manifest_version") or 1) >= 2:
        try:
            v2_summary = _apply_v2_bundle_sections(
                manifest=manifest_obj.content,
                local_library_id=local_library_id,
                upstream_node_id=upstream_node_id,
                tenant_id=tenant_id,
            )
        except Exception as e:
            log.warning("v2 bundle section apply failed for mirror %s: %s",
                        mirror_id, e)

    _record_mirror_synced(
        mirror_id, manifest_hash,
        pulled=pulled, failed=failed, bytes_pulled=bytes_pulled,
    )
    result = {"pulled": pulled, "failed": failed, "bytes": bytes_pulled}
    if v2_summary:
        result["v2"] = v2_summary
    return result


def _apply_v2_bundle_sections(
    *,
    manifest: dict,
    local_library_id: int,
    upstream_node_id: str,
    tenant_id: str,
) -> dict:
    """Apply the v2 bundle sections (entities, relationships,
    federation_id). Documents are already pulled by the caller via the
    existing per-doc HTTP path. Extractions[] is left for
    extraction_subscriber to discover via /fleet/extractions/find.

    Returns: {entities_created, entities_existing, relationships_inserted,
              federation_id_set, errors}.
    """
    import hashlib, json as _json, re
    from storage.postgres.db import get_connection

    federation_id = manifest.get("federation_id")
    entities = manifest.get("entities") or []
    relationships = manifest.get("relationships") or []
    notes_payload = manifest.get("notes") or []

    summary = {
        "entities_created":       0,
        "entities_existing":      0,
        "relationships_inserted": 0,
        "notes_inserted":         0,
        "notes_skipped":          0,
        "federation_id_set":      False,
        "errors":                 0,
    }

    with get_connection() as conn:
        with conn.cursor() as cur:
            # Bind the local library row to the federation_id so future
            # syncs from any peer holding the same federation_id mirror
            # the same logical library. Idempotent.
            if federation_id:
                cur.execute(
                    """UPDATE libraries
                          SET federation_id = COALESCE(federation_id, %s::uuid)
                        WHERE library_id = %s
                          AND (federation_id IS NULL OR federation_id::text = %s)""",
                    (federation_id, local_library_id, federation_id),
                )
                summary["federation_id_set"] = cur.rowcount > 0

            # ─── Entities ─────────────────────────────────────────────
            # Each entity is content-addressed by canonical_entity_key.
            # Lookup local; create at full GDU canonical (trust producer)
            # if missing. NEVER trust producer-supplied entity_id.
            entity_id_map: dict[str, int] = {}
            for e in entities:
                ckey = e.get("canonical_entity_key")
                name = (e.get("canonical_name") or "").strip()
                etype = (e.get("canonical_type") or "").strip()
                if not ckey or len(ckey) != 64 or not name:
                    continue
                # G8: re-verify the producer's ckey claim
                norm_name = re.sub(r"\s+", " ", name.lower()).strip()
                expected_ckey = hashlib.sha256(
                    f"{norm_name}|{etype.lower().strip()}".encode("utf-8")
                ).hexdigest()
                if expected_ckey != ckey:
                    summary["errors"] += 1
                    continue

                # Subtype lockdown — refuse to mirror peer rows whose
                # canonical_type isn't an admittable entityvocab subtype.
                # Peers may have legacy 'gpe'/'concept'/etc. entities; we
                # don't propagate their hygiene problems into our entities
                # table (where migration 116 would reject the INSERT
                # anyway). Skipped entities are not added to entity_id_map,
                # so any mention/relationship referencing them is also
                # skipped downstream. accept→normalize and proceed.
                try:
                    from core.ldu.subtype_validator import validate_for_ldu
                    _v = validate_for_ldu(etype)
                    if _v.action == "accept":
                        # Keep the producer's ckey unchanged — it's
                        # content-addressed by their name+type. Only
                        # normalize the locally-stored canonical_type
                        # ('org' → 'organization') so the row satisfies
                        # migration 116's CHECK.
                        if _v.canonical_subtype and _v.canonical_subtype != etype:
                            etype = _v.canonical_subtype
                    else:
                        summary.setdefault("entities_rejected_subtype", 0)
                        summary["entities_rejected_subtype"] += 1
                        log.info(
                            "library_subscriber: skipping peer entity %r "
                            "(type=%r, action=%s)", name, etype, _v.action,
                        )
                        continue
                except Exception:
                    log.debug("subtype_validator failed for peer entity %r",
                              name, exc_info=True)

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
                    entity_id_map[ckey] = row[0]
                    summary["entities_existing"] += 1
                else:
                    # normalized_name is NOT NULL on the entities table.
                    # Convention (from node-A's data): lower-cased canonical_name.
                    # We compute it locally rather than trusting the producer
                    # so two peers always agree on the same row's
                    # normalized_name regardless of locale quirks.
                    normalized = norm_name  # already lower+trim+single-space
                    # admitted_under_active_set is NOT NULL with no default
                    # on the entities table; carry the producer's value (if
                    # present) or default to a federation-pulled marker.
                    active_set = (e.get("admitted_under_active_set")
                                  or "pulled_v1")
                    cur.execute(
                        """
                        INSERT INTO entities (
                            tenant_id, canonical_name, canonical_type,
                            normalized_name,
                            canonical_entity_key, base60_address,
                            signal_count, gdu_canonical,
                            admitted_under_active_set,
                            pulled_from_node_id, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW()
                        )
                        RETURNING entity_id
                        """,
                        (
                            tenant_id, name[:500], etype[:128],
                            normalized[:500],
                            ckey, e.get("base60_address"),
                            int(e.get("signal_count") or 0),
                            bool(e.get("gdu_canonical")),
                            active_set[:32],
                            upstream_node_id,
                        ),
                    )
                    entity_id_map[ckey] = cur.fetchone()[0]
                    summary["entities_created"] += 1

            # ─── Relationships ────────────────────────────────────────
            # entity_relationships has UNIQUE on
            #   (tenant_id, source_entity_id, target_entity_id, relationship)
            # so re-syncs are idempotent via ON CONFLICT.
            for r in relationships:
                src_ckey = r.get("source_ckey")
                tgt_ckey = r.get("target_ckey")
                rel = (r.get("relationship") or "")[:64]
                if not (src_ckey and tgt_ckey and rel):
                    continue
                src = entity_id_map.get(src_ckey)
                tgt = entity_id_map.get(tgt_ckey)
                if src is None or tgt is None:
                    continue  # FK didn't resolve locally
                try:
                    cur.execute(
                        """
                        INSERT INTO entity_relationships (
                            tenant_id, source_entity_id, target_entity_id,
                            relationship, confidence, source, evidence,
                            is_current, article_count,
                            pulled_from_node_id, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s,
                            TRUE, 1,
                            %s, NOW(), NOW()
                        )
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            tenant_id, src, tgt, rel,
                            _clamp_conf(r.get("confidence")),
                            (r.get("source") or "federation_pull")[:64],
                            (r.get("evidence") or "")[:1000] or None,
                            upstream_node_id,
                        ),
                    )
                    if cur.rowcount:
                        summary["relationships_inserted"] += 1
                except Exception:
                    summary["errors"] += 1

        conn.commit()

    # ─── Notes (Phase 6) ──────────────────────────────────────────────
    # apply_pulled_notes opens its own transaction so a note-batch
    # failure can't roll back the entity/relationship inserts above.
    if notes_payload:
        try:
            from core.federation.notes import apply_pulled_notes
            nresult = apply_pulled_notes(
                notes_payload=notes_payload,
                local_library_id=local_library_id,
                upstream_node_id=upstream_node_id,
                tenant_id=tenant_id,
            )
            summary["notes_inserted"] = nresult["inserted"]
            summary["notes_skipped"]  = nresult["skipped_dupe"] + nresult["skipped_invalid"]
            if nresult["skipped_invalid"]:
                summary["errors"] += nresult["skipped_invalid"]
        except Exception as e:
            log.warning("apply_pulled_notes failed: %s", e)
            summary["errors"] += 1

    log.info(
        "v2 bundle applied for library %s: entities_new=%d existing=%d "
        "relationships_new=%d notes_new=%d notes_skipped=%d "
        "federation_id_set=%s errors=%d",
        local_library_id, summary["entities_created"], summary["entities_existing"],
        summary["relationships_inserted"], summary["notes_inserted"],
        summary["notes_skipped"], summary["federation_id_set"],
        summary["errors"],
    )
    return summary


def _clamp_conf(v):
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, f))


def _pull_and_store_document(
    *,
    client,                  # httpx.Client
    base_url: str,
    content_sha256: str,
    upstream_meta: dict,
    local_library_id: int,
    tenant_id: str,
) -> int:
    """Pull one document from upstream, verify, write to local corpus,
    insert documents row + bind to library. Returns bytes downloaded."""
    from core.enki.ingest import enki_path_for_hash
    from storage.postgres.db import get_connection

    url = f"{base_url}/p2p/content/sha256:{content_sha256}"
    # `client` is constructed in _sync_mirror with auth headers baked in,
    # so peer JWT travels with this call automatically.
    resp = client.get(url)
    if resp.status_code != 200:
        raise RuntimeError(f"p2p/content returned {resp.status_code}")
    body = resp.content

    # Content-addressed verification — any mismatch means corruption or
    # hostile upstream; discard the bytes without writing anywhere.
    actual = hashlib.sha256(body).hexdigest()
    if actual != content_sha256:
        raise RuntimeError(
            f"sha256 mismatch: claimed {content_sha256[:16]} got {actual[:16]}"
        )

    # Write the .enki blob to local corpus storage (immutable, addressed
    # by hash — collision-safe to overwrite)
    target_path = enki_path_for_hash(content_sha256, library_id=local_library_id)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_bytes(body)

    # Insert documents row + bind to library. Idempotent — if another
    # process beat us to it, the unique constraint on content_sha256
    # (or document_id depending on the schema vintage) catches it.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (
                    tenant_id, content_sha256, title, source_url,
                    on_disk_path, source_type, trust_class, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (content_sha256) DO UPDATE
                  SET on_disk_path = EXCLUDED.on_disk_path
                RETURNING document_id
                """,
                (
                    tenant_id,
                    content_sha256,
                    upstream_meta.get("source_title") or upstream_meta.get("title") or "",
                    upstream_meta.get("source_url") or "",
                    str(target_path),
                    "p2p_mirror",
                    "mentioned",
                ),
            )
            row = cur.fetchone()
            new_doc_id = row[0] if row else None

            if new_doc_id is not None:
                cur.execute(
                    """
                    INSERT INTO library_members (library_id, member_kind, member_id, added_at, added_by)
                    VALUES (%s, 'document', %s, NOW(), 'library_subscriber')
                    ON CONFLICT (library_id, member_kind, member_id) DO NOTHING
                    """,
                    (local_library_id, new_doc_id),
                )
        conn.commit()

    return len(body)


def _record_mirror_synced(
    mirror_id: int,
    manifest_hash: str,
    *,
    pulled: int,
    failed: int,
    bytes_pulled: int,
) -> None:
    """Update library_mirrors after a successful sync pass."""
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE library_mirrors
                   SET manifest_hash      = %s,
                       last_synced_at     = NOW(),
                       last_sync_error    = NULL,
                       documents_pulled   = documents_pulled + %s,
                       documents_failed   = documents_failed + %s,
                       bytes_pulled       = bytes_pulled + %s
                 WHERE mirror_id = %s
                """,
                (manifest_hash, pulled, failed, bytes_pulled, mirror_id),
            )
        conn.commit()


def _record_mirror_error(mirror_id: int, err: str) -> None:
    """Stamp an error on the mirror row; subscriber stays in 'active' so
    we'll retry next tick, but the UI surfaces the failure."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE library_mirrors
                       SET last_sync_error = %s,
                           documents_failed = documents_failed + 1
                     WHERE mirror_id = %s
                    """,
                    (str(err)[:500], mirror_id),
                )
            conn.commit()
    except Exception:
        log.exception("failed to record mirror error for %s", mirror_id)
