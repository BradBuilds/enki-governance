-- 075_context_bindings.sql
-- Learnable anaphora resolution: maps placeholders ("the house", "Al",
-- "the island") to specific entity_id by context. The substrate's memory
-- of what generic references mean.
--
-- Same placeholder in different documents binds to different entities.
-- Bindings accumulate evidence; human overrides win.

CREATE TABLE IF NOT EXISTS context_bindings (
    binding_id          BIGSERIAL PRIMARY KEY,
    tenant_id           VARCHAR(64)  NOT NULL DEFAULT 'default',

    -- The thing being resolved (lowercased, stripped).
    placeholder         TEXT         NOT NULL,

    -- What scope the binding applies to.
    --   'document' — context_value is a document_id (most precise)
    --   'library'  — context_value is a library_id (corpus-wide)
    --   'topic'    — context_value is an entity_id (e.g. all docs about Acme)
    --   'global'   — context_value NULL (system-wide default; rarely used)
    context_scope       VARCHAR(16)  NOT NULL,
    context_value       TEXT,

    -- The entity this placeholder resolves to in this context.
    resolved_entity_id  BIGINT       NOT NULL REFERENCES entities(entity_id),

    -- How we learned this binding.
    --   'doc_singleton'   — doc has exactly one specific candidate of the right type
    --   'paragraph_anchor'— same paragraph names the candidate
    --   'llm'             — Gemma disambiguated for us
    --   'human_override'  — user said this is correct (highest trust)
    --   'inferred_alias'  — short form matches alias of a known entity
    learned_from        VARCHAR(32)  NOT NULL,

    -- 0..1; human_override = 1.0, doc_singleton ~ 0.85, paragraph_anchor ~ 0.9
    confidence          REAL         NOT NULL DEFAULT 0.7,

    -- How many statements have used this binding.
    evidence_count      INTEGER      NOT NULL DEFAULT 1,

    -- Optional: who/what wrote it.
    created_by          VARCHAR(64),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- A single binding per (placeholder, scope, value) — re-asserting it
    -- bumps evidence_count + confidence instead of inserting a duplicate.
    UNIQUE (tenant_id, placeholder, context_scope, context_value)
);

CREATE INDEX IF NOT EXISTS idx_bindings_placeholder
    ON context_bindings (tenant_id, placeholder);
CREATE INDEX IF NOT EXISTS idx_bindings_lookup
    ON context_bindings (tenant_id, placeholder, context_scope, context_value);
CREATE INDEX IF NOT EXISTS idx_bindings_entity
    ON context_bindings (resolved_entity_id);


-- Audit log: every time the runtime resolver applies a binding, we record
-- which statement got resolved against which binding. Lets a human trace
-- backward from a row in the timeline → the binding → the evidence that
-- created the binding.
CREATE TABLE IF NOT EXISTS context_binding_applications (
    application_id      BIGSERIAL PRIMARY KEY,
    binding_id          BIGINT      NOT NULL REFERENCES context_bindings(binding_id) ON DELETE CASCADE,
    statement_id        BIGINT      REFERENCES statements(statement_id),
    assessment_id       BIGINT      REFERENCES assessments(assessment_id),
    applied_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_binding_apps_assessment
    ON context_binding_applications (assessment_id);
CREATE INDEX IF NOT EXISTS idx_binding_apps_binding
    ON context_binding_applications (binding_id);


-- The well-known placeholder set. Extracted strings that match these are
-- candidates for binding rather than promotion to new entities. Lowercased
-- match. Editable from the governance UI as we learn new ones.
CREATE TABLE IF NOT EXISTS anaphora_placeholders (
    placeholder         TEXT         PRIMARY KEY,
    expected_type       VARCHAR(64),       -- 'facility' | 'person' | 'organization' | NULL=any
    notes               TEXT,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

INSERT INTO anaphora_placeholders (placeholder, expected_type, notes) VALUES
    ('house',        'facility',     'generic residence reference'),
    ('the house',    'facility',     'generic residence reference'),
    ('his house',    'facility',     'generic residence reference'),
    ('her house',    'facility',     'generic residence reference'),
    ('the home',     'facility',     'generic residence reference'),
    ('home',         'facility',     'generic residence reference'),
    ('residence',    'facility',     'generic residence reference'),
    ('the property', 'facility',     'generic property reference'),
    ('property',     'facility',     'generic property reference'),
    ('ranch',        'facility',     'generic ranch reference'),
    ('the ranch',    'facility',     'generic ranch reference'),
    ('island',       'facility',     'generic island reference'),
    ('the island',   'facility',     'generic island reference'),
    ('the compound', 'facility',     'generic compound reference'),
    ('compound',     'facility',     'generic compound reference'),
    ('the building', 'facility',     'generic building reference'),
    ('building',     'facility',     'generic building reference'),
    ('the room',     'facility',     'generic room reference'),
    ('room',         'facility',     'generic room reference'),
    ('the office',   'facility',     'generic office reference'),
    ('office',       'facility',     'generic office reference')
ON CONFLICT (placeholder) DO NOTHING;
