-- 127_ckey_wikidata_id.sql
--
-- Recognize the enricher's `wikidata_id` key spelling as a Wikidata QID, in BOTH
-- the strongest-identifier precedence and the canonical_entity_key derivation.
--
-- Found by the whitepaper held-out evaluation: 223 real entities carry their QID
-- under the key `wikidata_id` (e.g. DoD = Q11208), but the resolver + ckey only
-- looked for `qid`/`wikidata` — so QID-based collapse and id-folding were silently
-- skipped for them (id-recall capped at the CIK-only fraction). This mirrors the
-- Python fix in core/federation/identity.py byte-for-byte so the trigger and
-- Python ckey stay in sync (data_integrity.check_ckey_python_sql_agree).
--
-- NOTE (clean-DB): existing wikidata_id rows carry a stale ckey (computed without
-- the id). They re-stamp on next write; a full re-stamp is only needed on the
-- current dirty grid, not on a fresh ingest where every row is computed under this
-- definition.

CREATE OR REPLACE FUNCTION compute_canonical_entity_key(
    p_name        text,
    p_type        text,
    p_identifiers jsonb DEFAULT NULL
) RETURNS char(64) AS $$
DECLARE
    v_name  text := trim(regexp_replace(lower(coalesce(p_name, '')), '\s+', ' ', 'g'));
    v_etype text := trim(lower(coalesce(p_type, '')));
    v_prec  text[] := ARRAY[
        'qid','wikidata','wikidata_id','lei','cik','duns','isin','permid',
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
                v_idtok := (CASE WHEN v_key IN ('wikidata','wikidata_id') THEN 'qid'
                                 ELSE v_key END)
                           || ':' || lower(trim(v_val));
                EXIT;
            END IF;
        END LOOP;
    END IF;
    RETURN encode(sha256((v_name || '|' || v_etype || '|' || v_idtok)::bytea), 'hex');
END;
$$ LANGUAGE plpgsql IMMUTABLE;
