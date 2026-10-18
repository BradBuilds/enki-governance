-- Migration 040: derived analytics for the watching system
--
-- Phase 4.7 of the collapse rebuild plan. ``node_metrics`` is the
-- output of the watch_derivatives worker — frequency, spike factor,
-- source entropy, hype score, hotspot intensity. It's READ-ONLY for
-- everything except the worker; not under GDU governance because the
-- rows are computed, not observed.
--
-- The table is keyed by node_id and refreshed in place. Older snapshots
-- are not kept here — set ``window_label`` to differentiate (e.g.
-- '24h', '7d') and run multiple windows against the same node_id.

CREATE TABLE IF NOT EXISTS node_metrics (
    node_id                 VARCHAR(255) NOT NULL,
    tenant_id               VARCHAR(255) NOT NULL DEFAULT 'default',
    window_label            VARCHAR(16)  NOT NULL DEFAULT '24h',

    -- Frequency / velocity
    mention_count           INTEGER      NOT NULL DEFAULT 0,
    mentions_per_hour       NUMERIC(10,3) NOT NULL DEFAULT 0,
    velocity                NUMERIC(10,3) NOT NULL DEFAULT 0,
    spike_factor            NUMERIC(10,3) NOT NULL DEFAULT 0,
    frequency_strip         INTEGER[]    DEFAULT '{}',  -- 24 hourly buckets

    -- Source diversity
    source_entropy          NUMERIC(8,4) NOT NULL DEFAULT 0,
    dominant_source         VARCHAR(64),
    dominant_source_share   NUMERIC(5,4) NOT NULL DEFAULT 0,
    hype_score              NUMERIC(8,4) NOT NULL DEFAULT 0,

    -- Place-specific (NULL when node_type != 'place')
    hotspot_intensity       NUMERIC(8,4),

    -- Computed at
    computed_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    PRIMARY KEY (node_id, window_label)
);

CREATE INDEX IF NOT EXISTS idx_node_metrics_tenant_window
    ON node_metrics (tenant_id, window_label);

CREATE INDEX IF NOT EXISTS idx_node_metrics_spike
    ON node_metrics (tenant_id, window_label, spike_factor DESC);

CREATE INDEX IF NOT EXISTS idx_node_metrics_hype
    ON node_metrics (tenant_id, window_label, hype_score DESC);

CREATE INDEX IF NOT EXISTS idx_node_metrics_hotspot
    ON node_metrics (tenant_id, window_label, hotspot_intensity DESC NULLS LAST);

INSERT INTO schema_migrations (version, applied_at)
VALUES ('040_node_metrics', NOW())
ON CONFLICT DO NOTHING;
