-- 124_canonical_entity_key_trigger.sql
--
-- Make canonical_entity_key (the cross-node identity primitive) computed by a
-- TRIGGER on every insert/update, so NO write path can leave it NULL or stale.
--
-- WHY
-- ---
-- Before this, the key was: (a) backfilled once by migration 104's SQL, and
-- (b) recomputed ad-hoc in three Python copies (entity_publisher.canonical_key,
-- extraction_subscriber._canonical_key, cards_api). New rows from the many
-- entity-insert paths (gateway, promotion, fission, subscribers) got NO key
-- unless a path happened to set it. That breaks federation: an entity with a
-- NULL key never matches its twin on another node.
--
-- Now the trigger stamps it on INSERT and on any UPDATE that touches the
-- source columns. One enforcement point, every path covered — the same pattern
-- as the canonical_type constraint.
--
-- FORMULA — mirrors core/federation/identity.py byte-for-byte:
--   sha256('<ws-normalized lower name>|<lower type>|<strongest_id>')
-- The strongest identifier (first present by precedence) is folded in so two
-- same-named entities with different strong ids (two "Acme"s, CIK 111 vs 222)
-- get distinct keys and coexist, while differently-named entities stay apart
-- even when a (possibly bad) id is shared. data_integrity.check_ckey_python_
-- sql_agree() asserts the Python and this function stay identical.

-- Remove any earlier 2-arg version so calls resolve unambiguously to the
-- identifier-aware function below.
DROP FUNCTION IF EXISTS compute_canonical_entity_key(text, text);

-- entities_pending_unique was UNIQUE (tenant_id, normalized_name) for pending
-- rows — it predates the identifier-aware key and WRONGLY forces two genuinely
-- different entities that share a name but differ by strong identifier (two
-- companies both "Acme", CIK 111 vs 222) to collide. The id-aware
-- idx_entities_canonical_key (tenant_id, canonical_entity_key) is the correct,
-- more precise uniqueness guarantee and supersedes it.
DROP INDEX IF EXISTS entities_pending_unique;

CREATE OR REPLACE FUNCTION compute_canonical_entity_key(
    p_name        text,
    p_type        text,
    p_identifiers jsonb DEFAULT NULL
) RETURNS char(64) AS $$
DECLARE
    v_name  text := trim(regexp_replace(lower(coalesce(p_name, '')), '\s+', ' ', 'g'));
    v_etype text := trim(lower(coalesce(p_type, '')));
    v_prec  text[] := ARRAY[
        'qid','wikidata','lei','cik','duns','isin','permid',
        'imo','mmsi','icao24','tail_number','registration',
        'cage','ncage','fec_id','bioguide_id','ror','grid','isni','orcid','ein'
    ];
    v_key   text;
    v_val   text;
    v_idtok text := '';
BEGIN
    IF p_identifiers IS NOT NULL AND jsonb_typeof(p_identifiers) = 'object' THEN
        FOREACH v_key IN ARRAY v_prec LOOP
            v_val := p_identifiers->>v_key;
            IF v_val IS NOT NULL AND length(trim(v_val)) > 0
               AND lower(trim(v_val)) NOT IN ('null','none','n/a') THEN
                v_idtok := (CASE WHEN v_key = 'wikidata' THEN 'qid' ELSE v_key END)
                           || ':' || lower(trim(v_val));
                EXIT;
            END IF;
        END LOOP;
    END IF;
    RETURN encode(sha256((v_name || '|' || v_etype || '|' || v_idtok)::bytea), 'hex');
END;
$$ LANGUAGE plpgsql IMMUTABLE;


CREATE OR REPLACE FUNCTION trg_set_canonical_entity_key() RETURNS trigger AS $$
BEGIN
    NEW.canonical_entity_key := compute_canonical_entity_key(
        NEW.canonical_name, NEW.canonical_type, NEW.identifiers);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


DROP TRIGGER IF EXISTS set_canonical_entity_key ON entities;
CREATE TRIGGER set_canonical_entity_key
    BEFORE INSERT OR UPDATE OF canonical_name, canonical_type, identifiers
    ON entities
    FOR EACH ROW
    EXECUTE FUNCTION trg_set_canonical_entity_key();

-- SEQUENCING — apply ONLY on a deduped/fresh DB.
-- idx_entities_canonical_key is UNIQUE (tenant_id, canonical_entity_key). On a
-- dirty DB with un-deduped duplicate (name|type) rows, stamping their keys via
-- the trigger collides on that unique index. On the live node-A this surfaced as 25
-- duplicate groups hiding behind NULL keys. So this migration is part of the
-- CLEAN-SLATE bring-up (run after enki-wipe-data.sh), or after Phase 1B's
-- entity_merge has collapsed all duplicate groups. We deliberately do NOT
-- backfill existing rows here — on a fresh DB there are none, and the trigger
-- populates every row going forward. (For an in-place upgrade of dirty data,
-- run entity_merge --apply first, then a guarded recompute.)

COMMENT ON FUNCTION compute_canonical_entity_key(text, text, jsonb) IS
  'Cross-node entity identity key. Mirrors core/federation/identity.py '
  'sha256(name|type|strongest_id). Trigger-maintained on entities. '
  'data_integrity.check_ckey_python_sql_agree() asserts Python==SQL.';
