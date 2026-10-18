-- 099_concept_address_sequences_no_upper_bound.sql
--
-- Drop the next_slot <= 60 CHECK constraint on concept_address_sequences.
--
-- Why: existing depth-2 data on production has slots up to 907 in
-- entity.person.<role> — the original cascade allocator in phase 8.9
-- wrote slots without going through concept_address_sequences, so the
-- sequences row was never created and the slot number escaped the cap.
-- Commit a4c39cf (hash-bucket fallback at depth-2) now drives all
-- allocations through the sequence, which means the bootstrap seeder
-- (scripts/seed_concept_address_sequences.py) needs to write next_slot
-- values that reflect actual usage. Capping at 60 prevents that.
--
-- Base-60 sparsity is enforced going forward via the depth-3 cascade
-- mechanism (Phase 8.10, future). The lower bound (next_slot >= 0) is
-- retained.
--
-- Reversible: re-add the constraint when depth-3 cascade is in place
-- and existing data has been wiped (Phase 4 of the restart plan).

ALTER TABLE concept_address_sequences
  DROP CONSTRAINT IF EXISTS concept_address_sequences_next_slot_check;

ALTER TABLE concept_address_sequences
  ADD CONSTRAINT concept_address_sequences_next_slot_check
  CHECK (next_slot >= 0);
