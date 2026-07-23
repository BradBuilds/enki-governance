-- 134: Validated, context-scoped entity aliases
-- Aliases learned by the governed alias-collapse (e.g. flight-log "AA" → Alex
-- Acme) are CONTEXT-DEPENDENT: "AA" only means Acme inside the flight-log
-- corpus, not globally. So they must NOT go into the global entities.aliases[]
-- array (which the resolver matches everywhere) — that would make every future
-- "AA" match Acme. This table records each learned alias with its SCOPE, its
-- confidence, and the equation evidence that validated it — auditable, reversible,
-- and correctly bounded to the context it was proven in.

CREATE TABLE IF NOT EXISTS entity_aliases (
    alias_id          BIGSERIAL PRIMARY KEY,
    tenant_id         VARCHAR(64) NOT NULL DEFAULT 'default',
    entity_id         BIGINT NOT NULL REFERENCES entities(entity_id) ON DELETE CASCADE,
    surface_form      TEXT NOT NULL,              -- raw surface as written ("AA")
    normalized        TEXT NOT NULL,              -- normalized form ("aa")
    scope_kind        VARCHAR(16) NOT NULL DEFAULT 'library',  -- global | library | document
    scope_id          BIGINT,                     -- library_id / document_id; NULL for global
    confidence        REAL NOT NULL DEFAULT 0.0,  -- collapse score-derived
    evidence          JSONB NOT NULL DEFAULT '{}',-- {axes:{}, margin, policy, cooccur_top}
    validation_status VARCHAR(32) NOT NULL DEFAULT 'validated_by_collapse',
        -- validated_by_collapse | review | operator | rejected
    source            VARCHAR(64) NOT NULL DEFAULT 'alias_collapse',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One validated binding per (surface, scope, entity). Re-runs update, not duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_aliases_scope
    ON entity_aliases (tenant_id, normalized, scope_kind, COALESCE(scope_id, -1), entity_id);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_lookup
    ON entity_aliases (tenant_id, normalized, scope_kind, scope_id);
CREATE INDEX IF NOT EXISTS idx_entity_aliases_entity
    ON entity_aliases (entity_id);
