-- 140: record which investigation sources REFUSED to answer.
--
-- sources_completed conflated two different facts: "this source was queried and
-- returned nothing" and "this source never answered". Anything downstream that
-- counts independent sources (corroboration tiers, N-independent scoring) read
-- the second as the first, so a WAF-blocked registry silently strengthened a
-- negative it had never actually contributed to.
--
-- Additive and backfill-free: existing rows keep '{}' , which is the honest
-- reading for runs made before blocked-detection existed — we do not know which
-- of their sources were blocked and must not guess.
ALTER TABLE entity_investigations
    ADD COLUMN IF NOT EXISTS sources_blocked TEXT[] DEFAULT '{}';

COMMENT ON COLUMN entity_investigations.sources_blocked IS
    'Sources that refused to answer (WAF challenge, 403/429). A gap in coverage '
    '— NOT evidence of absence. Disjoint from sources_completed.';
