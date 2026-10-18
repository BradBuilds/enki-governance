-- Events + topics governance triggers.
--
-- The existing trg_enforce_governance only covered knowledge_nodes, and
-- trg_enforce_entities_governance only covered entities. events and topics
-- had no governance whatsoever, allowing 25,113 ungoverned canonical
-- events and 35 ungoverned canonical topics to land. This migration
-- extends the same demote-on-write pattern to both tables.
--
-- Pattern: if a row claims gdu_canonical=TRUE but has no base60_address
-- or has a type not in the relevant pack, demote it to non-canonical
-- and record the reason in the envelope. The row still lands (so we
-- don't break code paths that aren't ready), but it doesn't claim
-- governed status until it's properly addressed.

-- Self-heal required columns. base60_address was added to entities and
-- knowledge_nodes by migration 027 but never explicitly added to
-- events / topics — node-A acquired the column out-of-band before this
-- governance migration shipped, so a fresh-DB run errored on the
-- BEFORE UPDATE OF base60_address clause below. Add it now so the
-- migration is idempotent on both the legacy node-A path and fresh installs.
ALTER TABLE events ADD COLUMN IF NOT EXISTS base60_address VARCHAR(16);
ALTER TABLE topics ADD COLUMN IF NOT EXISTS base60_address VARCHAR(16);

-- ── events ────────────────────────────────────────────────────────────

-- events has no envelope column — demote silently. Reason is implicit:
-- gdu_canonical=FALSE AND base60_address IS NULL means "no address slot",
-- and event_type is the proposed subtype which the row preserves.
CREATE OR REPLACE FUNCTION enforce_events_governance()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.gdu_canonical = TRUE THEN
    IF NEW.base60_address IS NULL THEN
      NEW.gdu_canonical := FALSE;
    ELSIF NEW.event_type IS NOT NULL
       AND NOT EXISTS (
         SELECT 1 FROM gdu_pack_positions
          WHERE category = 'event'
            AND subtype  = NEW.event_type
       )
    THEN
      NEW.gdu_canonical := FALSE;
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_enforce_events_governance ON events;
CREATE TRIGGER trg_enforce_events_governance
  BEFORE INSERT OR UPDATE OF event_type, gdu_canonical, base60_address
  ON events
  FOR EACH ROW EXECUTE FUNCTION enforce_events_governance();

-- ── topics ────────────────────────────────────────────────────────────

CREATE OR REPLACE FUNCTION enforce_topics_governance()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.gdu_canonical = TRUE THEN
    IF NEW.base60_address IS NULL THEN
      NEW.gdu_canonical := FALSE;
      NEW.envelope := COALESCE(NEW.envelope, '{}'::jsonb)
                    || jsonb_build_object(
                         'enforce_topics_governance',
                         jsonb_build_object(
                           'demoted_at', NOW(),
                           'raw_subtype', NEW.canonical_type,
                           'reason', 'no_base60_address'
                         )
                       );
    ELSIF NEW.canonical_type IS NOT NULL
       AND NOT EXISTS (
         SELECT 1 FROM gdu_pack_positions
          WHERE category = 'topic'
            AND subtype  = NEW.canonical_type
       )
    THEN
      NEW.gdu_canonical := FALSE;
      NEW.envelope := COALESCE(NEW.envelope, '{}'::jsonb)
                    || jsonb_build_object(
                         'enforce_topics_governance',
                         jsonb_build_object(
                           'demoted_at', NOW(),
                           'raw_subtype', NEW.canonical_type,
                           'reason', 'canonical_type_not_in_pack'
                         )
                       );
    END IF;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_enforce_topics_governance ON topics;
CREATE TRIGGER trg_enforce_topics_governance
  BEFORE INSERT OR UPDATE OF canonical_type, gdu_canonical, base60_address
  ON topics
  FOR EACH ROW EXECUTE FUNCTION enforce_topics_governance();
