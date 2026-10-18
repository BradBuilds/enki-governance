-- Migration 114: rule-based topic library linking.
--
-- A topic library should auto-include every document that matches its
-- focal entities or keywords, regardless of which source corpus the doc
-- was originally ingested from. Without this, topic libraries are
-- container-based — a doc has to be manually added — and they miss
-- content that ARRIVES through other corpora.
--
-- Example: when the FBI Declassified Records corpus ingests a doc that
-- mentions Alex Acme, that doc should
-- automatically appear in the Acme topic library.
-- The topic_link_worker reads linking_rule per topic library, finds
-- matching docs, and writes library_members rows.
--
-- linking_rule shape (JSONB):
--   {
--     "focal_entity_ids": [101, 102, ...],
--     "focal_keywords":   ["Acme", "Doe", "Example Island"],
--     "match_on":         ["mentions", "title", "extracted_text"],
--     "min_match_count":  1
--   }
--
-- focal_entity_ids is the strongest match (binding to a canonical
-- entity). focal_keywords is the fallback when no canonical entity
-- exists yet for a concept (e.g. "LiDAR archaeology" as a topic).

ALTER TABLE libraries
    ADD COLUMN IF NOT EXISTS linking_rule JSONB,
    ADD COLUMN IF NOT EXISTS last_linked_at TIMESTAMPTZ;

COMMENT ON COLUMN libraries.linking_rule IS
    'Rule for auto-populating library_members from any document we have. '
    'JSONB with focal_entity_ids + focal_keywords + match_on. '
    'Topic libraries use this to cross-link content from source corpora. '
    'Source/corpus libraries leave this NULL (they own their content directly).';

COMMENT ON COLUMN libraries.last_linked_at IS
    'When the topic_link_worker last ran on this library. Used to '
    'incrementally scan only documents newer than last_linked_at.';

CREATE INDEX IF NOT EXISTS idx_libraries_with_linking_rule
    ON libraries (last_linked_at)
    WHERE linking_rule IS NOT NULL;
