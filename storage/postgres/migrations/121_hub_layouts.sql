-- 121_hub_layouts.sql
--
-- Per-user Hub bucket configuration. Replaces the hardcoded "/feed?limit=5"
-- single-list view with operator-configurable sections (Military, Tech,
-- World, Cars, etc.). Each row stores one user's full Hub layout.
--
-- Each bucket is a small JSON object:
--   { "name": "Military", "categories": ["defense"], "top_n": 2, "order": 0 }
-- Multiple source categories can map to a single bucket (e.g. "Cyber"
-- bucket -> categories: ["cyber"]; "Geopolitics" -> ["world_news","political"]).
--
-- user_id is nullable; a row with user_id IS NULL is the tenant default
-- shown to any operator who hasn't customized their own layout.

CREATE TABLE IF NOT EXISTS hub_layouts (
    layout_id     BIGSERIAL PRIMARY KEY,
    tenant_id     VARCHAR(255) NOT NULL,
    user_id       VARCHAR(255),
    buckets       JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One layout per (tenant, user). user_id NULL = tenant default.
CREATE UNIQUE INDEX IF NOT EXISTS idx_hub_layouts_tenant_user
    ON hub_layouts (tenant_id, COALESCE(user_id, ''));

CREATE INDEX IF NOT EXISTS idx_hub_layouts_tenant
    ON hub_layouts (tenant_id);

-- Seed a tenant default for 'default' tenant so the Hub has buckets to
-- render even before any operator opens settings. Mirrors the four buckets
-- of the default layout: Military, Tech, World, Cars.
INSERT INTO hub_layouts (tenant_id, user_id, buckets)
VALUES (
    'default',
    NULL,
    '[
        {"name": "Military", "categories": ["defense"], "top_n": 2, "order": 0},
        {"name": "Tech",     "categories": ["tech"],    "top_n": 2, "order": 1},
        {"name": "World",    "categories": ["world_news"], "top_n": 2, "order": 2},
        {"name": "Cars",     "categories": ["auto"],    "top_n": 2, "order": 3}
    ]'::jsonb
)
ON CONFLICT DO NOTHING;
