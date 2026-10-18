"""
.enki ingest wrapper — every document going through the system gets
wrapped as a .enki file on disk, content-addressed, signed by this
node's Ed25519 key.

Usage from any ingest worker:

    from core.enki.ingest import ingest_document
    result = ingest_document(
        body=raw_bytes,
        source_url="https://...",
        source_title="Sky News article",
        mime_type="text/html",
        acquired_via="rss",
        tenant_id="default",
        library_id=5,  # optional
    )
    # result has: content_sha256, enki_path, document_id

The wrapper:
  1. Computes content_sha256
  2. Checks if we already have this content (dedup by hash)
  3. If new: signs a .enki file, writes to disk under storage/enki/...
  4. Inserts (or updates) the documents row with content_sha256 + enki_path
  5. If library_id given: inserts library_members row
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from core.enki.format import (
    build_document, load_enki, privkey_from_bytes, generate_keypair,
    privkey_to_bytes, pubkey_to_bytes, EnkiObject,
)
from storage.postgres.db import get_connection

log = logging.getLogger("enki.ingest")


# ── Node-key loading ────────────────────────────────────────────────────────

# Node Ed25519 signing key path. Default lives in /var/lib/enki/node_key.bin
# inside the container, or wherever ENKI_NODE_KEY_PATH points. Generated
# on first call if missing.
_NODE_KEY_CACHE = None


def _node_key_path() -> Path:
    return Path(os.environ.get(
        "ENKI_NODE_KEY_PATH",
        "/var/lib/enki/node_key.bin",
    ))


def get_node_signing_key():
    """Load (or generate) this node's Ed25519 private signing key."""
    global _NODE_KEY_CACHE
    if _NODE_KEY_CACHE is not None:
        return _NODE_KEY_CACHE

    path = _node_key_path()
    if path.exists():
        try:
            data = path.read_bytes()
            _NODE_KEY_CACHE = privkey_from_bytes(data)
            return _NODE_KEY_CACHE
        except Exception as e:
            log.error("failed to load node key from %s: %s", path, e)
            raise

    # Generate + persist.
    log.info("generating new node signing key at %s", path)
    pk = generate_keypair()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(privkey_to_bytes(pk))
    try:
        path.chmod(0o600)
    except Exception:
        pass
    _NODE_KEY_CACHE = pk
    return pk


def get_node_id() -> str:
    return os.environ.get("ENKI_NODE_ID", "node-unknown")


# ── .enki storage paths ────────────────────────────────────────────────────

def _enki_storage_root() -> Path:
    """Where .enki files live on disk. Configurable via env."""
    return Path(os.environ.get(
        "ENKI_STORAGE_ROOT",
        "/var/lib/enki/storage",
    ))


def enki_path_for_hash(content_sha256: str, library_id: Optional[int] = None) -> Path:
    """Deterministic path: storage/enki/[<library_id>/]<aa>/<aabbccdd...>.enki

    library_id is optional — if given, file lives under that library's
    subtree. If omitted, file lives under 'shared/'.
    """
    root = _enki_storage_root() / "enki"
    if library_id is not None:
        root = root / f"lib-{library_id}"
    else:
        root = root / "shared"
    hex_only = content_sha256.split(":", 1)[-1]
    return root / hex_only[:2] / f"{hex_only}.enki"


# ── Ingest API ─────────────────────────────────────────────────────────────

@dataclass
class IngestResult:
    document_id: Optional[int]
    content_sha256: str        # without "sha256:" prefix
    enki_path: str
    was_already_present: bool  # True if dedup hit
    library_member_added: bool


def ingest_document(
    *,
    body: bytes,
    source_url: str,
    source_title: Optional[str] = None,
    mime_type: str = "text/plain",
    acquired_via: str = "ingest",
    tenant_id: str = "default",
    library_id: Optional[int] = None,
    tags: Optional[list] = None,
    extra_metadata: Optional[dict] = None,
    dsn: Optional[str] = None,
) -> IngestResult:
    """Wrap a document body as .enki, write to disk, register in DB.

    Idempotent on content_sha256 — re-ingesting the same body produces
    the same result without writing twice.
    """
    if not body:
        raise ValueError("body cannot be empty")

    content_sha256_full = "sha256:" + hashlib.sha256(body).hexdigest()
    hex_only = content_sha256_full.split(":", 1)[-1]
    on_disk = enki_path_for_hash(content_sha256_full, library_id=library_id)

    already_present = False
    document_id: Optional[int] = None

    # ── If the .enki file already exists, dedup. ─────────────────────────
    if on_disk.exists():
        already_present = True
        log.debug("ingest dedup hit: %s already on disk", hex_only)
    else:
        # Build + sign + write the .enki file.
        privkey = get_node_signing_key()
        enki = build_document(
            body=body,
            source_url=source_url,
            source_title=source_title,
            mime_type=mime_type,
            acquired_by_node=get_node_id(),
            acquired_via=acquired_via,
            tags=tags,
            extra_metadata=extra_metadata,
            signer_privkey=privkey,
        )
        enki.write(on_disk)
        log.info("wrote .enki %s (%d bytes, %s)", on_disk, len(body), source_url[:80])

    # ── Upsert the documents row ─────────────────────────────────────────
    try:
        with get_connection(dsn) as conn:
            with conn.cursor() as cur:
                # First check if this hash is already in `documents`.
                cur.execute(
                    """SELECT document_id FROM documents
                        WHERE content_sha256 = %s
                        LIMIT 1""",
                    (hex_only,),
                )
                row = cur.fetchone()
                if row:
                    document_id = int(row[0])
                else:
                    # Insert a new row. We use the minimal subset of columns
                    # we KNOW are there (the documents table varies by
                    # deploy). Title + url + content_sha256 + enki_path are
                    # the core.
                    pubkey_hex = pubkey_to_bytes(
                        get_node_signing_key().public_key()
                    ).hex()
                    cur.execute(
                        """
                        INSERT INTO documents
                          (tenant_id, title, source_url,
                           content_sha256, enki_path,
                           enki_signed_at, enki_signer_pubkey,
                           on_disk_path,
                           created_at)
                        VALUES (%s, %s, %s, %s, %s, NOW(), %s, %s, NOW())
                        RETURNING document_id
                        """,
                        (
                            tenant_id,
                            (source_title or "(untitled)")[:500],
                            source_url[:2000],
                            hex_only,
                            str(on_disk),
                            pubkey_hex,
                            str(on_disk),
                        ),
                    )
                    document_id = int(cur.fetchone()[0])

                library_member_added = False
                if library_id is not None and document_id is not None:
                    cur.execute(
                        """
                        INSERT INTO library_members
                          (library_id, member_kind, member_id, added_by)
                        VALUES (%s, 'document', %s, 'enki.ingest')
                        ON CONFLICT DO NOTHING
                        """,
                        (library_id, document_id),
                    )
                    library_member_added = bool(cur.rowcount)
            conn.commit()
    except Exception as e:
        log.exception("ingest db write failed for %s: %s", hex_only, e)
        # The .enki file is on disk so the content isn't lost.
        return IngestResult(
            document_id=None,
            content_sha256=hex_only,
            enki_path=str(on_disk),
            was_already_present=already_present,
            library_member_added=False,
        )

    return IngestResult(
        document_id=document_id,
        content_sha256=hex_only,
        enki_path=str(on_disk),
        was_already_present=already_present,
        library_member_added=library_member_added,
    )


def load_document_enki(content_sha256: str,
                       library_id: Optional[int] = None) -> EnkiObject:
    """Load + verify a .enki file by content hash."""
    p = enki_path_for_hash(
        f"sha256:{content_sha256}" if not content_sha256.startswith("sha256:")
            else content_sha256,
        library_id=library_id,
    )
    if not p.exists() and library_id is not None:
        # Try shared bucket as fallback.
        p2 = enki_path_for_hash(
            f"sha256:{content_sha256}" if not content_sha256.startswith("sha256:")
                else content_sha256,
            library_id=None,
        )
        if p2.exists():
            p = p2
    return load_enki(p)


# ── Air-gap bundle import ──────────────────────────────────────────────────

def import_enki_file(
    path: Path,
    *,
    library_id: Optional[int] = None,
    tenant_id: str = "default",
    dsn: Optional[str] = None,
) -> IngestResult:
    """Import a single .enki file (e.g., from a USB bundle).
    Verifies signature + content_hash. Registers in DB. Copies into
    storage tree under the deterministic path.

    Use this for the air-gap import flow.
    """
    src = Path(path)
    obj = load_enki(src)  # verifies on load
    body = obj.content
    if not isinstance(body, (bytes, bytearray)):
        raise ValueError(f"expected document type, got type={obj.type}")
    md = obj.metadata or {}
    return ingest_document(
        body=bytes(body),
        source_url=md.get("source_url", ""),
        source_title=md.get("source_title"),
        mime_type=md.get("mime_type", "application/octet-stream"),
        acquired_via="bundle_import",
        tenant_id=tenant_id,
        library_id=library_id,
        tags=list(md.get("tags") or []),
        extra_metadata={
            "imported_from_path": str(src),
            "original_acquired_by_node": md.get("acquired_by_node"),
            "original_acquired_at": md.get("acquired_at"),
            "original_signer_pubkey": obj.signer_pubkey_hex,
        },
        dsn=dsn,
    )
