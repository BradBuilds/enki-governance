# SPINE S1 — Error Code Registry (LOCKED)

**Lock Date:** 2026-01-05  
**Lock Commit:** `a4b3c32dc9cf10946683973b1233f4922351877a`  
**Git Tag:** `SPINE-S1-LOCKED`  
**Status:** 🔒 **IMMUTABLE**

This document is the **single source of truth** for all SPINE S1 validation error codes. These codes are governance-locked and require RFC + 2 engineer approvals to modify.

---

## Spine-Level Validation Error Codes (HALT Semantics)

These error codes represent **validation contract violations** at the spine layer (S1.1-S1.5). All have HALT semantics (fail-closed, no fallback).

### Spine S1.0 — Active Set Enforcement (LOCKED 2026-01-05)

| Code | Layer | Severity | Required Context Keys | Description |
|------|-------|----------|----------------------|-------------|
| `PACK_NOT_IN_ACTIVE_SET` | S1.0 | HALT | `pack_id`, `active_set_version`, `active_set_mode` | Pack not permitted by active set (spine enforcement) |

### Spine S1.1-S1.5 — Validation Layers (LOCKED 2026-01-05)

| Code | Layer | Severity | Required Context Keys | Description |
|------|-------|----------|----------------------|-------------|
| `MISSING_CAPABILITY` | S1.1 | HALT | `consumer_pack_id`, `capability`, `version` | Capability requirement cannot be satisfied (0 providers) |
| `AMBIGUOUS_CAPABILITY` | S1.1 | HALT | `consumer_pack_id`, `capability`, `version`, `providers` | Multiple packs provide same capability+version |
| `CAPABILITY_VERSION_MISMATCH` | S1.1 | HALT | `consumer_pack_id`, `capability`, `required_version`, `provided_version` | Provider version doesn't match requirement |
| `NAMESPACE_COLLISION` | S1.2 | HALT | `pack_id`, `namespace`, `conflicting_pack_id`, `conflicting_namespace` | Two packs claim overlapping namespaces |
| `NAMESPACE_OWNERSHIP_VIOLATION` | S1.2 | HALT | `pack_id`, `definition_id`, `owned_namespaces` | Definition uses namespace not owned by pack |
| `NAMESPACE_INVALID_FORMAT` | S1.2 | HALT | `pack_id`, `namespace`, `pattern` | Namespace doesn't match required pattern |
| `DEFINITION_WITHOUT_NAMESPACE` | S1.2 | HALT | `pack_id`, `definition_id` | Pack defines ID but owns no namespaces |
| `PACK_HASH_MISMATCH` | S1.3 | HALT | `pack_id`, `declared_hash`, `computed_hash`, `pack_path` | Declared content hash doesn't match computed hash |
| `ACTIVATION_REQUIRED` | S1.5 | HALT | `active_set_version`, `transcript_hash` | No activation record found for active set |
| `ACTIVATION_TRANSCRIPT_MISMATCH` | S1.5 | HALT | `activation_id`, `declared_hash`, `computed_hash` | Activation references wrong transcript hash |

**Total Spine Validation Codes:** 11 HALT codes (S1.0-S1.5, all governance-locked)

---

## Pack Loader Error Codes (Non-Governance)

These error codes are part of the pack loading infrastructure but are **not spine validation contracts**. They represent file I/O, JSON parsing, and schema validation errors. While locked in implementation, they are not governance-level validation invariants.

**Important Distinction:** These errors are HALT by implementation necessity, but they do not represent semantic validation contracts and therefore do not participate in spine-level determinism guarantees or transcript hash stability. They validate pack structure, not semantic correctness.

| Code | Module | Severity | Description |
|------|--------|----------|-------------|
| `PACK_MANIFEST_MISSING` | loader | HALT | pack.json file not found |
| `PACK_MANIFEST_INVALID_JSON` | loader | HALT | pack.json is not valid JSON |
| `PACK_MANIFEST_INVALID_SCHEMA` | loader | HALT | pack.json doesn't match schema |
| `PACK_PATH_NOT_FOUND` | loader | HALT | Pack directory doesn't exist |
| `PACK_PATH_NOT_DIRECTORY` | loader | HALT | Pack path is not a directory |
| `DEFINITION_FILE_MISSING` | loader | HALT | Definition file not found |
| `DEFINITION_INVALID_JSON` | loader | HALT | Definition is not valid JSON |
| `DEFINITION_SCHEMA_INVALID` | loader | HALT | Definition doesn't match schema |
| `DUPLICATE_DEFINITION_ID` | loader | HALT | Two definitions have same ID |
| `PACK_EMPTY` | loader | HALT | Pack contains no definitions |
| `INVALID_CAPABILITY_DECLARATION` | loader | HALT | Capability declaration malformed |
| `DUPLICATE_CAPABILITY_DECLARATION` | loader | HALT | Pack declares capability twice |

**Note:** These codes validate pack structure but are not part of the spine validation contract (S1.0-S1.5).

---

## Governance Rules

### Locked Invariants

1. **Error Code Names** — Immutable strings (e.g., `MISSING_CAPABILITY`)
2. **Severity Semantics** — All spine codes are HALT (fail-closed)
3. **Required Context** — Context keys listed above are mandatory
4. **Sort Order** — Errors sort by `(error_code, pack_id, primary_ref, message)`
5. **Error Messages** — Human-readable messages in `ERROR_MESSAGES` dict can be improved for clarity without RFC

### Prohibited Changes (RFC Required)

- ❌ Renaming error codes (breaks log parsing, monitoring, audit queries)
- ❌ Removing error codes (breaks backward compatibility)
- ❌ Changing HALT → WARN severity (weakens fail-closed semantics)
- ❌ Removing required context keys (loses audit trail completeness)
- ❌ Adding new spine-level error codes without specification

### Allowed Changes (No RFC)

- ✅ Improving error message text (human-readable strings only)
- ✅ Adding optional context keys (must be documented)
- ✅ Adding non-spine loader error codes (pack infrastructure only)
- ✅ Clarifying documentation and examples

---

## Error Code Addition Process

To add a new **spine-level validation error code** (S1.0-S1.5 scope):

1. **Open an issue** on this repository proposing the code (an RFC)
   - Justify need: What validation contract requires this code?
   - Impact analysis: What systems parse error codes?
   - Migration: How do existing logs/monitors handle new codes?

2. **Obtain Approvals**
   - 2 engineer approvals (minimum)
   - Security review (if affects audit trail)

3. **Implementation**
   - Add code to `core/packs/errors.py`
   - Add to this registry table
   - Update golden tests if error ordering changes
   - Document in spine specification

4. **Communication**
   - Announce to node operators
   - Update monitoring/alerting rules
   - Add to release notes

---

## Error Ordering (Deterministic Sort Key)

All spine errors sort by SPINE-00 §3.1 specification:

```python
(error.error_code, error.pack_id, error.primary_ref, error.message)
```

**Primary Reference Precedence:**
1. `definition_id` (S1.2 ownership violations)
2. `namespace` (S1.2 collisions)
3. `capability` (S1.1 resolution errors)
4. `declared_hash` (S1.3 hash mismatches)
5. `activation_id` (S1.5 activation errors)

This ordering is **governance-locked** and must remain deterministic for transcript hash computation.

---

## Validation Error Context Requirements

Every spine validation error must include:

### Mandatory Fields (All Errors)
- `error_code`: String (from table above)
- `pack_id`: String (which pack caused error)
- `file_path`: Optional[str] (file location if applicable)
- `message`: String (human-readable description)

### Layer-Specific Fields
See "Required Context Keys" column in tables above.

### Example Error Structure
```python
{
    "error_code": "NAMESPACE_COLLISION",
    "pack_id": "audit-pack",
    "namespace": "audit",
    "conflicting_pack_id": "audit-extended",
    "conflicting_namespace": "audit.event",
    "message": "Namespace 'audit' claimed by both audit-pack and audit-extended",
    "file_path": "packs/audit-pack/pack.json"
}
```

---

## Golden Test Protection

Error codes are protected by golden anti-regression tests:

- **test_golden_error_ordering_immutable()** — Verifies sort key determinism
- **test_golden_error_context_completeness()** — Verifies required context keys

Any change to error codes that breaks these tests requires RFC approval.

---

## Historical Changes

### 2026-01-05: Initial Lock
- **RFC:** N/A (initial governance lock)
- **Commit:** `a4b3c32dc9cf10946683973b1233f4922351877a`
- **Action:** Locked 11 spine validation error codes (S1.0, S1.1, S1.2, S1.3, S1.5)
- **Rationale:** Establish immutable validation contract for S1 foundation

---

## Related Documentation

- `core/packs/errors.py` — Canonical implementation

---

## Quick Reference Card

**Spine Validation Errors (HALT):** 11 codes  
**Pack Loader Errors (Infrastructure):** 12 codes  
**Sort Key:** `(error_code, pack_id, primary_ref, message)`  
**Modification Process:** RFC + 2 approvals  
**Golden Test:** `test_golden_error_ordering_immutable()`  

**⚠️ Critical:** All spine error codes are HALT semantics. No WARN or fallback behavior allowed.

---

**END OF ERROR CODE REGISTRY**
