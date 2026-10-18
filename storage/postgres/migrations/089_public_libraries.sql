-- 089_public_libraries.sql
--
-- Phase 9 of the internal ldu gdu and public network plan.
--
-- Lets a library opt into public visibility. Other ENKI nodes can then
-- subscribe to it and earn yield_score by processing its documents.
--
-- Three columns added to `libraries`:
--   is_public            — bool flag, gates discovery in /fleet endpoints
--   subscription_policy  — invite_only | open | yield_gated
--   min_yield_required   — for yield_gated policy, the floor a subscriber
--                          must clear (see Phase 11 yield accounting)
--
-- One new table `library_subscriptions` tracks who's processing what.

BEGIN;

-- ── libraries: opt-in columns ───────────────────────────────────────────────

ALTER TABLE libraries
  ADD COLUMN IF NOT EXISTS is_public BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE libraries
  ADD COLUMN IF NOT EXISTS subscription_policy VARCHAR(32) NOT NULL
    DEFAULT 'invite_only'
    CHECK (subscription_policy IN ('invite_only', 'open', 'yield_gated'));

ALTER TABLE libraries
  ADD COLUMN IF NOT EXISTS min_yield_required REAL NOT NULL DEFAULT 0.0;

-- A library that doesn't have its `is_public` flag set should be
-- invisible to /fleet/libraries/public, regardless of policy. Partial
-- index on the (small) set of public libraries keeps the lookup cheap.
CREATE INDEX IF NOT EXISTS idx_libraries_public
  ON libraries (subscription_policy)
  WHERE is_public = TRUE;


-- ── library_subscriptions ───────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS library_subscriptions (
  subscription_id    BIGSERIAL PRIMARY KEY,
  library_id         BIGINT NOT NULL REFERENCES libraries(library_id)
                       ON DELETE CASCADE,
  subscriber_node_id VARCHAR(128) NOT NULL,
  subscribed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- active | paused | revoked
  status             VARCHAR(32) NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','paused','revoked')),
  -- A subscriber's stated processing rate (e.g., "willing to process
  -- up to N docs per day"). Hub-side scheduler uses this to throttle
  -- work assignment. Optional; null = no self-limit.
  max_docs_per_day   INTEGER,
  -- Cached yield at subscription time — useful for yield_gated audits.
  yield_at_subscribe REAL,
  last_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
  notes              TEXT,
  UNIQUE (library_id, subscriber_node_id)
);

CREATE INDEX IF NOT EXISTS idx_lib_subs_node
  ON library_subscriptions (subscriber_node_id, status);

CREATE INDEX IF NOT EXISTS idx_lib_subs_library_active
  ON library_subscriptions (library_id)
  WHERE status = 'active';

COMMIT;
