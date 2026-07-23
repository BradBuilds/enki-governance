-- 090_library_work_assignments.sql
--
-- Phase 10 of the internal ldu gdu and public network plan.
--
-- Hub-side table that tracks per-document work assignments to
-- subscribed nodes. The hub worker walks active subscriptions and
-- assigns each unprocessed doc to >= 2 distinct nodes so we can
-- cross-verify processing results (Phase 11).

BEGIN;

CREATE TABLE IF NOT EXISTS library_work_assignments (
  assignment_id       BIGSERIAL PRIMARY KEY,
  library_id          BIGINT NOT NULL REFERENCES libraries(library_id)
                        ON DELETE CASCADE,
  document_id         BIGINT NOT NULL,
  subscriber_node_id  VARCHAR(128) NOT NULL,
  -- assigned | in_progress | completed | failed | timeout
  status              VARCHAR(32) NOT NULL DEFAULT 'assigned'
                        CHECK (status IN
                          ('assigned','in_progress','completed','failed','timeout')),
  assigned_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  acknowledged_at     TIMESTAMPTZ,
  completed_at        TIMESTAMPTZ,
  -- Cross-verification anchor: SHA-256 of the canonical result
  -- envelope. Two subscribers reporting the same hash for the same
  -- (library_id, document_id) = consensus; mismatch = contested.
  result_hash         VARCHAR(64),
  -- Ed25519 signature over result_hash, signed by the subscriber's
  -- hardware key. Verifying the chain happens lazily in the yield
  -- accounting worker (Phase 11).
  result_signature    TEXT,
  -- Free-form notes from the subscriber (timing, errors, etc.).
  result_metadata     JSONB,
  -- A retry/escalation counter.
  attempts            INTEGER NOT NULL DEFAULT 0,
  UNIQUE (library_id, document_id, subscriber_node_id)
);

-- Hot path: hub assigning new work — needs to know which (library,
-- doc) pairs already have N assignments.
CREATE INDEX IF NOT EXISTS idx_lwa_lib_doc
  ON library_work_assignments (library_id, document_id);

-- Hot path: subscriber pulling its open assignments.
CREATE INDEX IF NOT EXISTS idx_lwa_node_open
  ON library_work_assignments (subscriber_node_id, status, assigned_at)
  WHERE status IN ('assigned', 'in_progress');

-- Cross-verification scan: walk completed assignments grouped by
-- (library, doc) to find consensus / contestation.
CREATE INDEX IF NOT EXISTS idx_lwa_completed
  ON library_work_assignments (library_id, document_id, status)
  WHERE status = 'completed';

COMMIT;
