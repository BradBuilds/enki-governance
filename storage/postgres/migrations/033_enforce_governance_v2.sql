-- Migration 033 — Smarter enforce_governance
--
-- Migration 032's enforce_governance refused ANY row with
-- gdu_canonical=TRUE AND base60_address IS NULL. That correctly blocks
-- new bypass leaks but ALSO blocks cleanup paths: when we delete a bad
-- row from entities, the FK ON DELETE SET NULL on
-- knowledge_nodes.master_entity_id triggers an UPDATE that touches the
-- existing bad row, which the trigger refuses.
--
-- Fix: only refuse the ASSERTION of canonical-without-address — not
-- pre-existing state being touched by housekeeping.
--
-- Refusal rules:
--   INSERT with gdu_canonical=TRUE AND base60_address IS NULL  → REFUSE
--   UPDATE transitioning gdu_canonical FALSE→TRUE without addr → REFUSE
--   UPDATE that leaves canonical/address state unchanged       → ALLOW
--   UPDATE that ADDS an address to a violating row             → ALLOW (remediation)
--   UPDATE that sets gdu_canonical=FALSE on a violating row    → ALLOW (drop to LDU-P)
--
-- Net effect: trigger blocks the leak forward but doesn't block cleanup
-- of existing violations.

BEGIN;

CREATE OR REPLACE FUNCTION enforce_governance()
RETURNS TRIGGER AS $$
BEGIN
    -- INSERT: strict check. No new canonical row without an address.
    IF TG_OP = 'INSERT' THEN
        IF NEW.gdu_canonical IS TRUE AND NEW.base60_address IS NULL THEN
            RAISE EXCEPTION
                'INSERT refused: ungoverned canonical row '
                'node_id=%, type=%.%. Either route through gateway.admit_node() '
                'to assign an address, or set gdu_canonical=FALSE for LDU-P.',
                NEW.node_id, NEW.node_type, NEW.subtype;
        END IF;
        RETURN NEW;
    END IF;

    -- UPDATE: only refuse when this update is what creates the violation.
    IF TG_OP = 'UPDATE' THEN
        -- If state already in violation and we're not changing canonical/addr → allow.
        IF OLD.gdu_canonical IS TRUE AND OLD.base60_address IS NULL THEN
            -- Pre-existing violation. Allow housekeeping that doesn't worsen it.
            -- The only thing we'd refuse is keeping it canonical without
            -- adding an address — but that's the same as before, so allow.
            RETURN NEW;
        END IF;

        -- Old row was OK (either canonical+addressed, or non-canonical).
        -- Refuse if NEW state asserts canonical without an address.
        IF NEW.gdu_canonical IS TRUE AND NEW.base60_address IS NULL THEN
            RAISE EXCEPTION
                'UPDATE refused: would create ungoverned canonical row '
                'node_id=%, type=%.%. Assign an address before setting '
                'gdu_canonical=TRUE, or keep gdu_canonical=FALSE.',
                NEW.node_id, NEW.node_type, NEW.subtype;
        END IF;

        RETURN NEW;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger definition unchanged from migration 032; CREATE OR REPLACE FUNCTION
-- means the trigger automatically picks up the new function body.

COMMIT;
