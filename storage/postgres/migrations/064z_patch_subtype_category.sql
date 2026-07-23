-- 064 patch: validator accepts EITHER subtype OR category match
--
-- The edgevocab declares source_types/target_types at the CATEGORY level
-- ("entity", "place", "event") but entities.canonical_type holds the
-- SUBTYPE ("person", "organization", "city"). Without this mapping the
-- validator rejects every edge whose endpoint type is a subtype name.
--
-- canonical_type_category() resolves a subtype to its category via
-- gdu_address_sequences (which already lists every (category, subtype)
-- the system knows about). validate_edge_type_pair() then accepts an
-- endpoint whose subtype name OR category name matches the contract.

CREATE OR REPLACE FUNCTION canonical_type_category(p_type TEXT) RETURNS TEXT AS $$
DECLARE
    cat TEXT;
BEGIN
    IF p_type IS NULL THEN RETURN NULL; END IF;
    SELECT category INTO cat
      FROM gdu_address_sequences
     WHERE subtype = p_type
     LIMIT 1;
    RETURN cat;
END;
$$ LANGUAGE plpgsql STABLE;


CREATE OR REPLACE FUNCTION validate_edge_type_pair(
    p_src_type TEXT,
    p_edge_type TEXT,
    p_dst_type TEXT
) RETURNS TEXT AS $$
DECLARE
    c gdu_type_constraints;
    src_cat TEXT := canonical_type_category(p_src_type);
    dst_cat TEXT := canonical_type_category(p_dst_type);
BEGIN
    SELECT * INTO c FROM gdu_type_constraints WHERE edge_type = p_edge_type;
    IF NOT FOUND THEN
        RETURN 'edge_type_unknown:' || COALESCE(p_edge_type, '<null>');
    END IF;
    IF p_src_type IS NULL OR NOT (
         p_src_type = ANY(c.source_types) OR
         src_cat    = ANY(c.source_types)
       ) THEN
        RETURN 'src_type_invalid:' || COALESCE(p_src_type, '<null>')
               || ' not in [' || array_to_string(c.source_types, ',') || ']';
    END IF;
    IF p_dst_type IS NULL OR NOT (
         p_dst_type = ANY(c.target_types) OR
         dst_cat    = ANY(c.target_types)
       ) THEN
        RETURN 'dst_type_invalid:' || COALESCE(p_dst_type, '<null>')
               || ' not in [' || array_to_string(c.target_types, ',') || ']';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;
