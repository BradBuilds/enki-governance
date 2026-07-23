-- Migration 007 — article_bookmarks
--
-- Lightweight "Remember" flag for articles/nodes. Not a research trigger,
-- not a validation path — just a personal pile the user can recall later.
-- Categories are free-form suggestions (idea / interesting / follow-up /
-- save); the default ('save') keeps the flow one click.
--
-- Keyed on knowledge_nodes.node_id so any envelope-shaped node is
-- bookmarkable (articles, observations, claims, whatever).

CREATE TABLE IF NOT EXISTS article_bookmarks (
    bookmark_id  BIGSERIAL PRIMARY KEY,
    tenant_id    VARCHAR(255) NOT NULL DEFAULT 'default',
    identity_id  VARCHAR(255) NOT NULL DEFAULT 'dev-user',
    node_id      VARCHAR(255) NOT NULL,
    category     VARCHAR(32)  NOT NULL DEFAULT 'save',
    note         TEXT,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT article_bookmarks_unique UNIQUE (tenant_id, identity_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_article_bookmarks_user
    ON article_bookmarks (tenant_id, identity_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_article_bookmarks_node
    ON article_bookmarks (node_id);

CREATE INDEX IF NOT EXISTS idx_article_bookmarks_category
    ON article_bookmarks (tenant_id, identity_id, category, created_at DESC);
