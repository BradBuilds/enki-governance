-- 081_topic_membership.sql
--
-- Topic membership layer.
--
-- Topics were already a first-class node category in nodevocab (v0.8.0,
-- 11 subtypes: conflict, operation, treaty, technology, ideology, etc.)
-- and the `topics` table has full GDU governance fields. What's missing
-- is the DECLARATIVE MEMBERSHIP LAYER — a way to say "this topic includes
-- these entities/events/assessments/documents/signals" computed from a
-- rule, not hand-tagged.
--
-- Design constraints:
--   * Generic — nothing in code knows what any specific topic is about.
--   * Membership is COMPUTED from a JSON rule (membership_rule), not
--     pre-declared.
--   * Same primitive serves: Roswell research library, live news watch
--     topics, financial crime cases, personal investigations, scientific
--     lit reviews, family trees. The substrate doesn't care.
--   * Topics are admitted through the gateway like every other node and
--     get base-60 addresses from the same address assigner.
--
-- This migration adds:
--   1. `membership_rule` JSONB column on `topics` — the declarative def.
--   2. `topic_membership` table — materialized members per topic.
--   3. Provenance fields (created_by, materialized_at, member_count) so
--      we can tell user-curated from auto-clustered from watch-driven.

BEGIN;

ALTER TABLE topics
  ADD COLUMN IF NOT EXISTS membership_rule JSONB,
  ADD COLUMN IF NOT EXISTS created_by TEXT NOT NULL DEFAULT 'manual',
  ADD COLUMN IF NOT EXISTS materialized_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS member_count INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN topics.membership_rule IS
  'Declarative JSON rule that, when evaluated, returns the topic''s members. '
  'Generic — same engine evaluates Roswell co-occurrence and Iran-Israel '
  'news watch and a financial-crime case file. No topic-specific code.';
COMMENT ON COLUMN topics.created_by IS
  'Provenance: manual | user | auto_cluster | watch_topic | cascade_seed | api';
COMMENT ON COLUMN topics.materialized_at IS
  'When membership was last materialized from the rule. NULL = never.';

CREATE INDEX IF NOT EXISTS idx_topics_created_by ON topics (created_by);
CREATE INDEX IF NOT EXISTS idx_topics_materialized_at ON topics (materialized_at);

-- Materialized membership.
--
-- One row per (topic, member). Member is polymorphic — `member_kind` tells
-- you which substrate table to look it up in. We deliberately don't add
-- FKs because we'd need per-kind FKs and the cleanup paths get gnarly;
-- soft links + per-kind audit run is cleaner.
--
-- Soft-delete via `evicted_at` so we can re-materialize without losing
-- history (and so we can compare rule revisions to see what changed).
CREATE TABLE IF NOT EXISTS topic_membership (
  topic_id     BIGINT NOT NULL REFERENCES topics(topic_id) ON DELETE CASCADE,
  member_kind  TEXT NOT NULL CHECK (member_kind IN
                 ('entity','event','assessment','document','signal','topic')),
  member_id    BIGINT NOT NULL,
  score        REAL NOT NULL DEFAULT 1.0,
  via_rule     TEXT,
  added_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  evicted_at   TIMESTAMPTZ,
  tenant_id    VARCHAR(64) NOT NULL DEFAULT 'default',
  PRIMARY KEY (topic_id, member_kind, member_id)
);

COMMENT ON TABLE topic_membership IS
  'Materialized topic membership. One row per (topic, member). Computed '
  'by the membership_rule evaluator. evicted_at lets re-materialization '
  'soft-delete without losing history.';
COMMENT ON COLUMN topic_membership.score IS
  'Confidence/weight that this member belongs to the topic. Rule '
  'evaluators set it; UIs use it for ranking.';
COMMENT ON COLUMN topic_membership.via_rule IS
  'Which clause of the membership_rule produced this membership. Lets '
  'us debug why something is in a topic and split rules cleanly.';

-- Fast lookup: which topics does this member belong to?
CREATE INDEX IF NOT EXISTS idx_tm_member
  ON topic_membership (member_kind, member_id) WHERE evicted_at IS NULL;

-- Fast topic-side ranking: top members of a topic by score
CREATE INDEX IF NOT EXISTS idx_tm_topic_score
  ON topic_membership (topic_id, score DESC) WHERE evicted_at IS NULL;

-- Convenience view: counts per kind per topic (for the UI badge)
CREATE OR REPLACE VIEW topic_member_counts AS
SELECT topic_id, member_kind, COUNT(*) AS n
  FROM topic_membership
 WHERE evicted_at IS NULL
 GROUP BY topic_id, member_kind;

COMMIT;
