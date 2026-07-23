-- 068: Purchase / borrow links on every reference + new source slots
--
-- Design intent: keep a permanent record of each reference so a reader
-- can buy or borrow the book, and link it to the node listing the book.
-- A reference is a permanent node even when we can't auto-download it.
-- For every reference we know exists, we persist standardized
-- acquisition links (Amazon, AbeBooks, OpenLibrary borrow, WorldCat,
-- Google Books preview, etc.) so the analyst always has a path to
-- actually obtaining it.

ALTER TABLE bibliographic_references
    ADD COLUMN IF NOT EXISTS purchase_links JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS discovered_via_secondary TEXT[] NOT NULL DEFAULT '{}';

-- Quick view: any reference + its top 5 purchase links
CREATE OR REPLACE VIEW reference_acquisition_options AS
SELECT r.reference_id,
       r.base60_address,
       r.title,
       r.acquisition_status,
       r.library_id,
       jsonb_array_length(r.purchase_links) AS link_count,
       r.purchase_links
  FROM bibliographic_references r;
