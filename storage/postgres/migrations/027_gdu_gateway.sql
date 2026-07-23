-- 027_gdu_gateway.sql
-- GDU Gateway: Base-60 addressing, address sequences, admission audit log
--
-- The GDU is the final collapsed form. Every node gets a base-60 address
-- that identifies its ontological position: [category].[subtype].[sequence].
-- The admission log hash-chains every gateway decision for tamper detection.

-- ── Base-60 address columns on knowledge_nodes ─────────────────────────
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS base60_address VARCHAR(16);
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS base60_category SMALLINT;
ALTER TABLE knowledge_nodes ADD COLUMN IF NOT EXISTS base60_subtype SMALLINT;

CREATE INDEX IF NOT EXISTS idx_nodes_base60
    ON knowledge_nodes (base60_address)
    WHERE base60_address IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_nodes_base60_cat
    ON knowledge_nodes (base60_category, base60_subtype)
    WHERE base60_category IS NOT NULL;

-- ── Base-60 address on entities ────────────────────────────────────────
ALTER TABLE entities ADD COLUMN IF NOT EXISTS base60_address VARCHAR(16);

CREATE INDEX IF NOT EXISTS idx_entities_base60
    ON entities (base60_address)
    WHERE base60_address IS NOT NULL;

-- ── Address sequence counters ──────────────────────────────────────────
-- One row per (category, subtype). Atomically incremented on each new node
-- to produce unique base-60 instance IDs within a slot.
CREATE TABLE IF NOT EXISTS gdu_address_sequences (
    category    VARCHAR(32)  NOT NULL,
    subtype     VARCHAR(64)  NOT NULL,
    next_seq    BIGINT       NOT NULL DEFAULT 1,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (category, subtype)
);

-- Seed the 9 categories with their subtypes from nodevocab
-- Entity subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('entity', 'person'), ('entity', 'organization'), ('entity', 'vessel'),
    ('entity', 'aircraft'), ('entity', 'vehicle'), ('entity', 'facility'),
    ('entity', 'account'), ('entity', 'device'), ('entity', 'unit'),
    ('entity', 'weapon_system'), ('entity', 'product')
ON CONFLICT DO NOTHING;

-- Place subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('place', 'country'), ('place', 'admin_region'), ('place', 'city'),
    ('place', 'poi'), ('place', 'facility_location'), ('place', 'coordinate_cluster'),
    ('place', 'named_area'), ('place', 'maritime_area'), ('place', 'airspace')
ON CONFLICT DO NOTHING;

-- Event subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('event', 'strike'), ('event', 'transaction'), ('event', 'arrival'),
    ('event', 'departure'), ('event', 'meeting'), ('event', 'statement'),
    ('event', 'broadcast'), ('event', 'publication'), ('event', 'alert'),
    ('event', 'sighting'), ('event', 'transfer'), ('event', 'earthquake'),
    ('event', 'weather'), ('event', 'appointment'), ('event', 'contract')
ON CONFLICT DO NOTHING;

-- Topic subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('topic', 'conflict'), ('topic', 'operation'), ('topic', 'treaty'),
    ('topic', 'protocol'), ('topic', 'technology'), ('topic', 'ideology'),
    ('topic', 'market'), ('topic', 'supply_chain'), ('topic', 'program'),
    ('topic', 'doctrine'), ('topic', 'investigation')
ON CONFLICT DO NOTHING;

-- Source subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('source', 'outlet'), ('source', 'feed'), ('source', 'account'),
    ('source', 'sensor'), ('source', 'connector'), ('source', 'dataset'),
    ('source', 'leak'), ('source', 'official'), ('source', 'ngo'),
    ('source', 'blog'), ('source', 'forum')
ON CONFLICT DO NOTHING;

-- Document subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('document', 'article'), ('document', 'article_live_blog'), ('document', 'image'),
    ('document', 'video'), ('document', 'pdf'), ('document', 'audio'),
    ('document', 'dataset_row'), ('document', 'leak_document'),
    ('document', 'satellite_tile'), ('document', 'transcript'),
    ('document', 'report'), ('document', 'social_post'), ('document', 'email'),
    ('document', 'press_release')
ON CONFLICT DO NOTHING;

-- Chronicle subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('chronicle', 'day'), ('chronicle', 'week'), ('chronicle', 'month'),
    ('chronicle', 'quarter'), ('chronicle', 'named_period')
ON CONFLICT DO NOTHING;

-- Assessment subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('assessment', 'fact'), ('assessment', 'observation'), ('assessment', 'claim'),
    ('assessment', 'hypothesis'), ('assessment', 'question'), ('assessment', 'recommendation')
ON CONFLICT DO NOTHING;

-- Pattern subtypes
INSERT INTO gdu_address_sequences (category, subtype) VALUES
    ('pattern', 'pattern_of_life'), ('pattern', 'recurring_meeting'),
    ('pattern', 'supply_route'), ('pattern', 'shell_company_chain'),
    ('pattern', 'communication_cluster'), ('pattern', 'financial_flow')
ON CONFLICT DO NOTHING;

-- ── GDU Admission Log ──────────────────────────────────────────────────
-- Hash-chained audit trail for every gateway decision.
-- Every admit, reject, route_ldup, or collapse is recorded here.
-- The hash chain allows tamper detection: if any row is modified or
-- deleted outside the gateway, the chain breaks.
CREATE TABLE IF NOT EXISTS gdu_admission_log (
    rid             BIGSERIAL    PRIMARY KEY,
    tenant_id       VARCHAR(255) NOT NULL,
    node_id         VARCHAR(255),
    entity_id       BIGINT,
    edge_id         VARCHAR(255),
    operation       VARCHAR(32)  NOT NULL,  -- admit, reject, route_ldup, collapse
    category        VARCHAR(32),
    subtype         VARCHAR(64),
    base60_address  VARCHAR(16),
    input_type      VARCHAR(64),            -- what the LLM called it
    mapped_type     VARCHAR(64),            -- what GDU mapped it to
    reason          TEXT,
    lineage_stamp   JSONB,
    mutation_hash   VARCHAR(64),
    previous_hash   VARCHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_gdu_admission_tenant
    ON gdu_admission_log (tenant_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_gdu_admission_operation
    ON gdu_admission_log (operation);

CREATE INDEX IF NOT EXISTS idx_gdu_admission_node
    ON gdu_admission_log (node_id)
    WHERE node_id IS NOT NULL;
