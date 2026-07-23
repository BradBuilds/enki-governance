-- Migration 069: widen watch_topic_entities.entity_type to varchar(64)
--
-- The sync_canonical_type_to_wte() trigger from migration 017 propagates
-- entities.canonical_type (varchar(64)) into watch_topic_entities.entity_type
-- (varchar(20)). Any canonical_type longer than 20 chars (government_agency,
-- military_facility, legal_filings, corporate_filings, etc.) blows up the
-- trigger with StringDataRightTruncation, which rolls back the entire
-- entity_validator transaction. Result: 4,753 of 11,322 entities (42%) stuck
-- as gdu_canonical=false; zero cross-document corroboration possible; zero
-- fact-tier assessments produced.
--
-- Fix: widen the target column to match the source. No data migration
-- needed — existing values are all <= 20 chars.

BEGIN;

ALTER TABLE watch_topic_entities
    ALTER COLUMN entity_type TYPE character varying(64);

COMMIT;
