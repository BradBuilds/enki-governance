-- 128_entity_tombstones.sql
--
-- Right-to-be-forgotten: erasing an entity must PERMANENTLY remove its PII while
-- leaving an auditable, federation-propagating record that the erasure happened —
-- WITHOUT retaining the erased content. The tombstone stores the content-address
-- (canonical_entity_key) and a HASH of the identity, never the name/identifiers,
-- so the address is retired and re-creation can be blocked, but no PII survives.
--
-- "Reversible" in the GDU sense = the erasure is a governed, logged, attributable
-- operation (not a silent DELETE). It is NOT reversible for the PII itself — that is
-- the point of forgetting.

CREATE TABLE IF NOT EXISTS entity_tombstones (
    tombstone_id     bigserial PRIMARY KEY,
    tenant_id        text NOT NULL DEFAULT 'default',
    canonical_entity_key char(64) NOT NULL,      -- the retired content-address
    identity_hash    char(64) NOT NULL,          -- sha256 of name|type — proves what, not who
    canonical_type   text,                       -- coarse type retained (not PII)
    erased_at        timestamptz NOT NULL DEFAULT now(),
    erased_by        text NOT NULL,              -- actor (attribution, audit)
    reason           text NOT NULL,              -- legal basis / request id
    origin_node_id   text,                       -- where the erasure was requested
    UNIQUE (tenant_id, canonical_entity_key)
);

-- fast admission check: "is this incoming entity's address tombstoned?"
CREATE INDEX IF NOT EXISTS idx_entity_tombstones_ckey
    ON entity_tombstones (tenant_id, canonical_entity_key);

COMMENT ON TABLE entity_tombstones IS
    'Right-to-be-forgotten records. Retires a content-address permanently and blocks '
    're-creation; retains no PII (only the ckey + an identity hash + coarse type).';
