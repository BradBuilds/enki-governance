"""
Library export — serialize one library + its members into a portable
JSON envelope that another ENKI install can ingest.

Frozen-core base-60 addresses (libraries/language/definitions/
language.frozen_addresses.json) plus deterministic concept addresses
plus Wikidata QID identifiers make the receiving side able to dedupe
without name reconciliation: same address means same thing.

Envelope shape:
{
  "envelope_version": "1.0",
  "exported_at": "2026-05-14T...",
  "source_install": "<install_id>",
  "library": { ... library row ... },
  "references": [ { ... bibliographic_references row ... }, ... ],
  "documents":  [ { ... documents row ... }, ... ],
  "entities":   [ { ... entities row ... }, ... ],
  "assessments": [ { ... assessments row ... }, ... ],
  "events": [ ... events row ... ],
  "statements": [ ... statements rows ... ],
  "citation_emissions": [ ... ],
  "signature": { "algorithm": "hmac-sha256", "value": "...",
                 "ed25519_pending": true }
}

The signature is HMAC-SHA256 for now (using a tenant-shared secret).
Hardware-key Ed25519 signing via the HWKEY infrastructure will replace
the HMAC layer when that integration is wired up.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, "/app")

from storage.postgres.db import get_connection

log = logging.getLogger("enki.discovery.library_export")


ENVELOPE_VERSION = "1.0"


def _row_to_dict(cur, row) -> dict:
    """Use psycopg2 description to produce dict-from-row."""
    cols = [d[0] for d in cur.description]
    out = {}
    for col, val in zip(cols, row):
        if isinstance(val, datetime):
            out[col] = val.isoformat()
        elif isinstance(val, (bytes, bytearray)):
            out[col] = val.hex()
        else:
            out[col] = val
    return out


def _fetch_all_dict(conn, sql: str, params: tuple = ()) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [_row_to_dict(cur, r) for r in cur.fetchall()]


def export_library(library_id: int, *, install_id: Optional[str] = None,
                   shared_secret: Optional[str] = None) -> dict:
    """Serialize one library + everything attached to it into a single
    envelope dict. Caller writes to disk / sends to network / etc.

    shared_secret enables HMAC-SHA256 over the canonical JSON; missing
    secret = unsigned envelope (verifiable structure only).
    """
    install = install_id or os.environ.get("ENKI_INSTALL_ID", "unknown")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM libraries WHERE library_id = %s", (library_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"library_id={library_id} not found")
            library_row = _row_to_dict(cur, row)

        refs = _fetch_all_dict(conn,
            "SELECT * FROM bibliographic_references WHERE library_id = %s "
            "ORDER BY reference_id", (library_id,))

        # Documents that belong to this library
        doc_ids: list[int] = []
        with conn.cursor() as cur:
            cur.execute(
                "SELECT member_id FROM library_members "
                "WHERE library_id = %s AND member_kind = 'document'",
                (library_id,),
            )
            doc_ids = [r[0] for r in cur.fetchall()]
        docs = _fetch_all_dict(conn,
            "SELECT * FROM documents WHERE document_id = ANY(%s)",
            (doc_ids,)) if doc_ids else []

        # Entities — pull every entity referenced by mentions in those docs
        entity_ids: list[int] = []
        if doc_ids:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT entity_id FROM document_mentions "
                    "WHERE document_id = ANY(%s) AND entity_id IS NOT NULL",
                    (doc_ids,),
                )
                entity_ids = [r[0] for r in cur.fetchall()]
        entities = _fetch_all_dict(conn,
            "SELECT * FROM entities WHERE entity_id = ANY(%s)",
            (entity_ids,)) if entity_ids else []

        # Assessments + supporting statements from this library's docs
        statement_ids: list[int] = []
        statements: list[dict] = []
        assessments: list[dict] = []
        if doc_ids:
            statements = _fetch_all_dict(conn,
                "SELECT * FROM statements WHERE document_id = ANY(%s)",
                (doc_ids,))
            statement_ids = [s["statement_id"] for s in statements]
            if statement_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM assessments "
                        " WHERE source_statement_ids && %s::bigint[]",
                        (statement_ids,),
                    )
                    assessments = [_row_to_dict(cur, r) for r in cur.fetchall()]

        # Events whose originating_statement_ids touch these statements
        events: list[dict] = []
        if statement_ids:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM events "
                    " WHERE originating_statement_ids && %s::bigint[]",
                    (statement_ids,),
                )
                events = [_row_to_dict(cur, r) for r in cur.fetchall()]

        # Citation emissions from these documents
        emissions: list[dict] = []
        if doc_ids:
            emissions = _fetch_all_dict(conn,
                "SELECT * FROM citation_emissions "
                " WHERE source_document_id = ANY(%s)",
                (doc_ids,))

    envelope = {
        "envelope_version":  ENVELOPE_VERSION,
        "exported_at":       datetime.now(timezone.utc).isoformat(),
        "source_install":    install,
        "address_contract":  "enki-base60-v1",
        "library":           library_row,
        "references":        refs,
        "documents":         docs,
        "entities":          entities,
        "assessments":       assessments,
        "events":            events,
        "statements":        statements,
        "citation_emissions": emissions,
    }

    canonical = _canonical_json(envelope)

    sig: dict[str, Any] = {
        "algorithm": "none",
        "value":     None,
        "ed25519_pending": True,  # HWKEY hardware-signing slot
    }
    if shared_secret:
        h = hmac.new(shared_secret.encode("utf-8"), canonical.encode("utf-8"),
                     hashlib.sha256).hexdigest()
        sig = {"algorithm": "hmac-sha256", "value": h,
               "ed25519_pending": True}
    envelope["signature"] = sig
    return envelope


def _canonical_json(envelope: dict) -> str:
    """Deterministic JSON for signing. Excludes the signature itself
    so signing/verification yield the same canonical bytes."""
    cleaned = {k: v for k, v in envelope.items() if k != "signature"}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), default=str)


def verify_envelope(envelope: dict, shared_secret: Optional[str]) -> tuple[bool, str]:
    sig = envelope.get("signature") or {}
    algo = sig.get("algorithm") or "none"
    if algo == "none":
        return (shared_secret is None,
                "unsigned envelope" if shared_secret is None
                else "envelope is unsigned but receiver expects a signature")
    if algo == "hmac-sha256":
        if not shared_secret:
            return False, "no shared_secret provided"
        canonical = _canonical_json(envelope)
        expected = hmac.new(shared_secret.encode("utf-8"),
                             canonical.encode("utf-8"),
                             hashlib.sha256).hexdigest()
        if hmac.compare_digest(expected, sig.get("value") or ""):
            return True, "hmac-sha256 ok"
        return False, "hmac-sha256 mismatch"
    return False, f"unsupported signature algorithm: {algo}"


def write_envelope(library_id: int, path: str, *,
                    install_id: Optional[str] = None,
                    shared_secret: Optional[str] = None) -> dict:
    env = export_library(library_id, install_id=install_id,
                          shared_secret=shared_secret)
    with open(path, "w") as fp:
        json.dump(env, fp, indent=2, default=str)
    return {
        "library_id": library_id,
        "path": path,
        "size_bytes": os.path.getsize(path),
        "counts": {
            "references":         len(env["references"]),
            "documents":          len(env["documents"]),
            "entities":           len(env["entities"]),
            "assessments":        len(env["assessments"]),
            "events":             len(env["events"]),
            "statements":         len(env["statements"]),
            "citation_emissions": len(env["citation_emissions"]),
        },
        "signed": env["signature"]["algorithm"] != "none",
    }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("library_id", type=int)
    p.add_argument("--out", required=True)
    p.add_argument("--secret", default=os.environ.get("ENKI_ENVELOPE_SECRET"))
    p.add_argument("--install", default=os.environ.get("ENKI_INSTALL_ID"))
    args = p.parse_args()
    print(json.dumps(write_envelope(args.library_id, args.out,
                                      install_id=args.install,
                                      shared_secret=args.secret),
                     indent=2))
