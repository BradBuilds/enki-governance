-- Migration 144 — the attestation gate. C3.4.
--
-- ############################################################################
-- #  DO NOT APPLY THIS YET.                                                  #
-- #                                                                          #
-- #  It is the LAST step of C3, and its precondition is a MEASUREMENT, not a #
-- #  code review: `scripts/verify_admission_chain.py --coverage` must report  #
-- #  zero unattested nodes created since the attestation deploy. As of        #
-- #  2026-08-06 it does not — `entity` and `topic` nodes minted by the        #
-- #  migration-032 mirror triggers are still unattested, and applying this    #
-- #  now would abort every transaction that creates an entity.                #
-- #                                                                          #
-- #  This file exists so the gate is designed while the evidence for it is    #
-- #  being gathered, not invented under pressure afterwards.                  #
-- ############################################################################
--
-- WHY A CONSTRAINT TRIGGER AND NOT `BEFORE INSERT`
--
-- Migration 032 set the precedent: `enforce_governance` is a BEFORE INSERT
-- trigger that makes "canonical row with no base-60 address" impossible from
-- anywhere — Python, SQL, manual psql, equally. That works because the property
-- it checks is visible in the row being inserted.
--
-- Attestation is not. The admission record and the node are two rows, written in
-- either order depending on the writer: `gateway.admit_node` logs after
-- `_ensure_knowledge_node`, `auto_node` attests after its upsert, and
-- `entity_promotion` attests after an `entities` INSERT whose mirror trigger
-- creates the node. A BEFORE INSERT check would fire before the attestation
-- exists and reject every legitimate write.
--
-- A DEFERRABLE INITIALLY DEFERRED constraint trigger fires at COMMIT, when both
-- rows are present regardless of the order they were written in. It is
-- ordering-agnostic by construction rather than by convention, which is the
-- whole point: a fifteenth writer tomorrow cannot get this wrong by writing its
-- two rows in an order nobody anticipated.
--
-- WHAT IT DOES NOT DO
--
-- It does not check that the attestation is TRUE — that a row claiming
-- `vocab: true` really ran the vocabulary check. Nothing at the database layer
-- can. It checks that a claim was MADE and hash-chained, which converts a silent
-- omission into a recorded statement someone can be held to. The hash chain plus
-- `scripts/verify_admission_chain.py` is what makes the statement tamper-evident
-- after the fact.
--
-- It deliberately does NOT require `chain_regime` to be non-legacy. A future
-- regime bump must not turn into an outage.

BEGIN;

CREATE OR REPLACE FUNCTION require_attestation()
RETURNS TRIGGER AS $$
BEGIN
    -- Deferred to commit: by now the writer's attest_admission() call has
    -- landed in the same transaction, whichever side of the node write it sat.
    IF NOT EXISTS (
        SELECT 1 FROM gdu_admission_log
         WHERE node_id = NEW.node_id
           AND tenant_id = NEW.tenant_id
    ) THEN
        RAISE EXCEPTION
            'Refusing unattested node: node_id=%, type=%.%. Admission provenance '
            'cannot be reconstructed after the fact, so it must be recorded with '
            'the write. Call core.gdu.attest.attest_admission() in the SAME '
            'transaction, stating which governance checks actually ran.',
            NEW.node_id, NEW.node_type, NEW.subtype;
    END IF;
    RETURN NULL;   -- AFTER trigger: return value is ignored
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_require_attestation ON knowledge_nodes;
CREATE CONSTRAINT TRIGGER trg_require_attestation
    AFTER INSERT ON knowledge_nodes
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION require_attestation();

COMMIT;

-- ROLLBACK, should ingestion break:
--   DROP TRIGGER IF EXISTS trg_require_attestation ON knowledge_nodes;
-- The attestation rows already written stay valid and chained; only the
-- refusal stops. Dropping the gate loses the guarantee, never the record.
