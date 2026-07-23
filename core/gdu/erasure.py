"""Right-to-be-forgotten — governed, auditable entity erasure.

`erase_entity` PERMANENTLY removes an entity's PII (name, aliases, identifiers, and
its links from mentions/assessments) and writes an `entity_tombstones` record that
retains NO PII — only the retired content-address (canonical_entity_key), a hash of
the identity, and the audit fields (who/when/why). `is_tombstoned` lets the admission
path refuse to re-create a forgotten entity, so erasure sticks even as new documents
arrive. The tombstone is what federates, so peer nodes erase too.

This is the GDU sense of "reversible": the operation is logged and attributable, not
a silent DELETE. The PII itself is gone — that is the point of forgetting.
"""
from __future__ import annotations

import hashlib


def _identity_hash(name: str | None, ctype: str | None) -> str:
    return hashlib.sha256(f"{(name or '').lower().strip()}|{(ctype or '').lower().strip()}"
                          .encode("utf-8")).hexdigest()


def is_tombstoned(cur, canonical_entity_key: str, tenant: str = "default") -> bool:
    """True if this content-address has been erased — admission must not re-create it."""
    if not canonical_entity_key:
        return False
    cur.execute("SELECT 1 FROM entity_tombstones WHERE tenant_id = %s "
                "AND canonical_entity_key = %s", (tenant, canonical_entity_key))
    return cur.fetchone() is not None


_UNLINK = (  # (table, column) entity references to sever before deleting the PII row
    ("document_mentions", "entity_id"),
    ("assessments", "subj_entity_id"),
    ("assessments", "obj_entity_id"),
)


def erase_entity(conn, entity_id: int, *, reason: str, erased_by: str,
                 origin_node_id: str | None = None) -> dict:
    """Permanently erase one entity. Returns a confirmation with the retired address.
    Idempotent on the tombstone (ON CONFLICT). Best-effort unlink of references."""
    with conn.cursor() as cur:
        cur.execute("""SELECT canonical_entity_key, canonical_name, canonical_type, tenant_id
                         FROM entities WHERE entity_id = %s""", (entity_id,))
        row = cur.fetchone()
        if not row:
            return {"ok": False, "error": "not_found", "entity_id": entity_id}
        ckey, name, ctype, tenant = row
        if not ckey:
            from core.gdu.identity import canonical_entity_key
            ckey = canonical_entity_key(name, ctype, None)

        cur.execute("""
            INSERT INTO entity_tombstones (tenant_id, canonical_entity_key, identity_hash,
                canonical_type, erased_by, reason, origin_node_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, canonical_entity_key) DO NOTHING
        """, (tenant, ckey, _identity_hash(name, ctype), ctype, erased_by, reason, origin_node_id))

        for table, col in _UNLINK:  # sever references without aborting on missing tables
            cur.execute("SAVEPOINT sp_unlink")
            try:
                cur.execute(f"UPDATE {table} SET {col} = NULL WHERE {col} = %s", (entity_id,))
                cur.execute("RELEASE SAVEPOINT sp_unlink")
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT sp_unlink")

        cur.execute("DELETE FROM entities WHERE entity_id = %s", (entity_id,))
        conn.commit()
        return {"ok": True, "entity_id": entity_id, "ckey": ckey, "tombstoned": True,
                "pii_removed": True, "reason": reason}
