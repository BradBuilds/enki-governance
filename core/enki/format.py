"""
.enki file format — CBOR-encoded, Ed25519-signed, content-addressed.

Every .enki file is a CBOR map with this structure:

  {
    "v":     1,                               # spec version
    "type":  "document" | "annotation" | "manifest" | "library_bundle",
    "content_hash":  "sha256:<hex>",          # address of the payload
    "content":       <type-specific payload>, # bytes / dict
    "metadata":      <dict>,                  # provenance + tags + ts
    "lineage":       <dict>,                  # parent_hashes etc.
    "signed_at":     <unix ts>,
    "signer_pubkey": <32-byte ed25519 pubkey, raw>,
    "signature":     <64-byte ed25519 sig>,
  }

The signature covers the CBOR-encoded payload of every field EXCEPT
"signature" itself. So:

  sig = ed25519_sign(privkey, cbor_encode(map_without_signature))

Verification recomputes that payload and verifies. Tampering with any
field — even one byte of the content — breaks the signature.

Naming on disk:
  <library_id>/<hash[:2]>/<hash>.enki

Read paths:
  load_enki(path) → EnkiObject       (verified on load by default)
  loads_enki(bytes) → EnkiObject

Write paths:
  build_document(body, metadata, signer_privkey) → EnkiObject
  build_annotation(...) → EnkiObject
  build_manifest(...) → EnkiObject
  obj.write(path)

For bundles (a manifest + every referenced .enki), use the
``pack_bundle`` / ``unpack_bundle`` helpers in core.enki.bundle.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union

import cbor2
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey, Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption,
)

SPEC_VERSION = 1
ENKI_FILE_SUFFIX = ".enki"

# ── Allowed type discriminator values ──────────────────────────────────────

TYPE_DOCUMENT       = "document"
TYPE_ANNOTATION     = "annotation"
TYPE_MANIFEST       = "manifest"
TYPE_LIBRARY_BUNDLE = "library_bundle"

VALID_TYPES = {
    TYPE_DOCUMENT,
    TYPE_ANNOTATION,
    TYPE_MANIFEST,
    TYPE_LIBRARY_BUNDLE,
}


# ── Errors ──────────────────────────────────────────────────────────────────

class EnkiFormatError(Exception):
    """Bad shape — missing field, wrong type discriminator, etc."""


class EnkiSignatureError(Exception):
    """Signature verification failed."""


class EnkiContentHashError(Exception):
    """content_hash doesn't match the actual payload."""


# ── The in-memory object ───────────────────────────────────────────────────

@dataclass
class EnkiObject:
    """Decoded .enki content. Use the build_* helpers to construct."""
    type: str
    content_hash: str             # "sha256:<hex>"
    content: Union[bytes, dict]
    metadata: dict = field(default_factory=dict)
    lineage: dict = field(default_factory=dict)
    signed_at: int = 0
    signer_pubkey: bytes = b""
    signature: bytes = b""

    # ── Encoding ──────────────────────────────────────────────────────────

    def _signed_payload(self) -> bytes:
        """The bytes that the signature covers — everything except `signature`."""
        return cbor2.dumps({
            "v":             SPEC_VERSION,
            "type":          self.type,
            "content_hash":  self.content_hash,
            "content":       self.content,
            "metadata":      self.metadata,
            "lineage":       self.lineage,
            "signed_at":     self.signed_at,
            "signer_pubkey": self.signer_pubkey,
        })

    def to_bytes(self) -> bytes:
        """Full CBOR-encoded .enki bytes (with signature)."""
        return cbor2.dumps({
            "v":             SPEC_VERSION,
            "type":          self.type,
            "content_hash":  self.content_hash,
            "content":       self.content,
            "metadata":      self.metadata,
            "lineage":       self.lineage,
            "signed_at":     self.signed_at,
            "signer_pubkey": self.signer_pubkey,
            "signature":     self.signature,
        })

    def write(self, path: Union[str, Path]) -> Path:
        """Atomic write to disk (write tmp then rename)."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(self.to_bytes())
        os.replace(tmp, p)
        return p

    # ── Verification ──────────────────────────────────────────────────────

    def verify(self) -> None:
        """Raise if signature or content_hash is wrong."""
        # 1. content_hash matches what's in content
        actual = compute_content_hash(self.content)
        if actual != self.content_hash:
            raise EnkiContentHashError(
                f"content_hash mismatch: stored={self.content_hash} "
                f"computed={actual}"
            )
        # 2. signature verifies
        try:
            pk = Ed25519PublicKey.from_public_bytes(self.signer_pubkey)
            pk.verify(self.signature, self._signed_payload())
        except InvalidSignature as e:
            raise EnkiSignatureError(str(e))

    # ── Convenience ───────────────────────────────────────────────────────

    @property
    def content_hash_hex(self) -> str:
        """Just the hex part (no sha256: prefix)."""
        return self.content_hash.split(":", 1)[-1]

    @property
    def signer_pubkey_hex(self) -> str:
        return self.signer_pubkey.hex()


# ── Hashing ────────────────────────────────────────────────────────────────

def compute_content_hash(content: Union[bytes, dict]) -> str:
    """SHA-256 of the CBOR-encoded content (deterministic).

    For bytes: just sha256(content).
    For dicts: sha256(cbor2.dumps(content, canonical=True)).
    """
    if isinstance(content, (bytes, bytearray)):
        h = hashlib.sha256(bytes(content)).hexdigest()
    else:
        h = hashlib.sha256(cbor2.dumps(content, canonical=True)).hexdigest()
    return f"sha256:{h}"


# ── Construction ───────────────────────────────────────────────────────────

def _build(
    *,
    type: str,
    content: Union[bytes, dict],
    metadata: Optional[dict],
    lineage: Optional[dict],
    signer_privkey: Ed25519PrivateKey,
) -> EnkiObject:
    if type not in VALID_TYPES:
        raise EnkiFormatError(f"invalid type: {type!r}")

    content_hash = compute_content_hash(content)
    obj = EnkiObject(
        type=type,
        content_hash=content_hash,
        content=content,
        metadata=dict(metadata or {}),
        lineage=dict(lineage or {}),
        signed_at=int(time.time()),
        signer_pubkey=signer_privkey.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw,
        ),
    )
    obj.signature = signer_privkey.sign(obj._signed_payload())
    return obj


def build_document(
    *,
    body: bytes,
    source_url: str,
    source_title: Optional[str] = None,
    mime_type: str = "application/octet-stream",
    acquired_by_node: str,
    acquired_via: str = "ingest",
    tags: Optional[list] = None,
    extra_metadata: Optional[dict] = None,
    signer_privkey: Ed25519PrivateKey,
) -> EnkiObject:
    """Build a .enki document file from raw bytes."""
    metadata = {
        "source_url":       source_url,
        "source_title":     source_title,
        "mime_type":        mime_type,
        "size_bytes":       len(body),
        "acquired_at":      int(time.time()),
        "acquired_by_node": acquired_by_node,
        "acquired_via":     acquired_via,
        "tags":             list(tags or []),
        **(extra_metadata or {}),
    }
    return _build(
        type=TYPE_DOCUMENT,
        content=body,
        metadata=metadata,
        lineage=None,
        signer_privkey=signer_privkey,
    )


def build_annotation(
    *,
    parent_document_hash: str,        # the content_hash of the doc this annotates
    annotation_type: str,             # 'extracts_entity' | 'extracts_claim' | 'disputes' | ...
    body: dict,                       # the structured claim
    source_offset_start: Optional[int],
    source_offset_end: Optional[int],
    source_quote: Optional[str],
    source_quote_hash: Optional[str], # sha256 of the quoted span
    extra_metadata: Optional[dict] = None,
    signer_privkey: Ed25519PrivateKey,
) -> EnkiObject:
    """Build a .enki annotation. The body is the structured claim; the
    metadata anchors it to a specific span of the parent document."""
    content = {
        "annotation_type":     annotation_type,
        "body":                body,
        "source_offset_start": source_offset_start,
        "source_offset_end":   source_offset_end,
        "source_quote":        source_quote,
        "source_quote_hash":   source_quote_hash,
    }
    metadata = {
        "created_at":          int(time.time()),
        **(extra_metadata or {}),
    }
    lineage = {"parent_hashes": [parent_document_hash]}
    return _build(
        type=TYPE_ANNOTATION,
        content=content,
        metadata=metadata,
        lineage=lineage,
        signer_privkey=signer_privkey,
    )


def build_manifest(
    *,
    library_id: int,
    library_canonical_name: str,
    manifest_version: int,
    documents: list,                  # [{content_hash, size_bytes, source_url, ...}, ...]
    extra_metadata: Optional[dict] = None,
    signer_privkey: Ed25519PrivateKey,
    # v2 bundle sections — pass when manifest_version >= 2 (Phase 5 of
    # library federation plan). All optional for backward compat with
    # v1 callers that only pass `documents`.
    entities: Optional[list] = None,            # [{canonical_entity_key, canonical_name, canonical_type, base60_address, evidence_doc_shas}, ...]
    extractions: Optional[list] = None,         # [{content_sha256, profile_fingerprint, producer_node_id, envelope_sha256, row_counts}, ...]
    relationships: Optional[list] = None,       # [{source_ckey, target_ckey, relationship, confidence}, ...]
    notes: Optional[list] = None,               # Phase 6 — empty for now
    federation_id: Optional[str] = None,        # UUID string, set once at first publish
    producer_node_id: Optional[str] = None,     # primary producer (this node)
    co_producers: Optional[list] = None,        # other peers that have contributed
) -> EnkiObject:
    """Build a .enki library manifest. The content is the sorted list
    of document content_hashes; the metadata holds the library identity
    and version. Receivers verify by checking each referenced
    content_hash against the documents in the bundle.

    manifest_version == 1: documents only (legacy). manifest_version == 2:
    full federation bundle — documents + entities + extractions +
    relationships + notes + federation_id. Subscribers detect via
    content["manifest_version"] and dispatch accordingly. A subscriber
    asked to consume a v2 manifest but only built for v1 should refuse
    rather than silently drop the new sections.
    """
    # Sort documents by content_hash so the manifest_root is deterministic.
    docs_sorted = sorted(documents, key=lambda d: d.get("content_hash", ""))
    content: dict = {
        "library_id":             library_id,
        "library_canonical_name": library_canonical_name,
        "manifest_version":       manifest_version,
        "document_count":         len(docs_sorted),
        "total_size_bytes":       sum(d.get("size_bytes", 0) for d in docs_sorted),
        "documents":              docs_sorted,
    }

    if manifest_version >= 2:
        # Determinism: sort every section by a stable key so two nodes
        # building the same logical manifest produce byte-identical
        # content_hash. Same principle as the extraction envelope v2.
        content["federation_id"]    = federation_id
        content["producer_node_id"] = producer_node_id
        content["co_producers"]     = sorted(co_producers or [])
        content["entities"] = sorted(
            entities or [], key=lambda e: e.get("canonical_entity_key", ""),
        )
        content["extractions"] = sorted(
            extractions or [],
            key=lambda x: (x.get("content_sha256", ""),
                            x.get("profile_fingerprint", ""),
                            x.get("producer_node_id", "")),
        )
        content["relationships"] = sorted(
            relationships or [],
            key=lambda r: (r.get("source_ckey", ""),
                            r.get("target_ckey", ""),
                            r.get("relationship", "")),
        )
        content["notes"] = sorted(notes or [], key=lambda n: n.get("note_sha256", ""))
        content["entity_count"]       = len(content["entities"])
        content["extraction_count"]   = len(content["extractions"])
        content["relationship_count"] = len(content["relationships"])
        content["note_count"]         = len(content["notes"])

    metadata = {
        "created_at": int(time.time()),
        **(extra_metadata or {}),
    }
    return _build(
        type=TYPE_MANIFEST,
        content=content,
        metadata=metadata,
        lineage=None,
        signer_privkey=signer_privkey,
    )


# ── Loading ────────────────────────────────────────────────────────────────

def loads_enki(data: bytes, *, verify: bool = True) -> EnkiObject:
    """Decode a .enki blob from bytes. Verifies signature + content_hash
    by default."""
    try:
        m = cbor2.loads(data)
    except Exception as e:
        raise EnkiFormatError(f"invalid CBOR: {e}")

    if not isinstance(m, dict):
        raise EnkiFormatError("top-level must be a map")
    if m.get("v") != SPEC_VERSION:
        raise EnkiFormatError(f"unsupported version: {m.get('v')!r}")

    try:
        obj = EnkiObject(
            type=m["type"],
            content_hash=m["content_hash"],
            content=m["content"],
            metadata=dict(m.get("metadata") or {}),
            lineage=dict(m.get("lineage") or {}),
            signed_at=int(m.get("signed_at") or 0),
            signer_pubkey=bytes(m["signer_pubkey"]),
            signature=bytes(m["signature"]),
        )
    except KeyError as e:
        raise EnkiFormatError(f"missing field: {e}")

    if obj.type not in VALID_TYPES:
        raise EnkiFormatError(f"invalid type: {obj.type!r}")

    if verify:
        obj.verify()
    return obj


def load_enki(path: Union[str, Path], *, verify: bool = True) -> EnkiObject:
    """Load + verify a .enki file from disk."""
    return loads_enki(Path(path).read_bytes(), verify=verify)


# ── Key helpers ────────────────────────────────────────────────────────────

def generate_keypair() -> Ed25519PrivateKey:
    """New Ed25519 private key. Public is privkey.public_key()."""
    return Ed25519PrivateKey.generate()


def privkey_to_bytes(privkey: Ed25519PrivateKey) -> bytes:
    """Raw 32-byte private key for on-disk storage. KEEP SECRET."""
    return privkey.private_bytes(
        Encoding.Raw, PrivateFormat.Raw, NoEncryption(),
    )


def privkey_from_bytes(b: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(b)


def pubkey_to_bytes(pubkey: Ed25519PublicKey) -> bytes:
    return pubkey.public_bytes(Encoding.Raw, PublicFormat.Raw)
