# Infrastructure Error Codes (Non-Governance)

**Status:** Implementation Reference  
**Last Updated:** 2026-01-05  
**Purpose:** Document infrastructure and warning codes used by pack loader and validators

This document lists error codes that are **implementation details** rather than governance-level spine validation contracts. These codes are not subject to RFC approval but should remain stable for operational continuity.

---

**Note:** S1.0 Active Set enforcement (PACK_NOT_IN_ACTIVE_SET) has been promoted to governance-locked status in ERROR_CODES_LOCKED.md as of 2026-01-05.

---

## Infrastructure Validation Codes

These codes extend the pack loader with additional validation checks beyond the governance-locked codes in ERROR_CODES_LOCKED.md.

| Code | Module | Severity | Description |
|------|--------|----------|-------------|
| `DEFINITION_MISSING_REQUIRED_FIELD` | loader | HALT | Definition missing a required field |
| `DUPLICATE_DEFINITION_NAME` | loader | HALT | Two definitions have the same name |
| `INVALID_STATUS_VALUE` | loader | HALT | Definition has invalid status value |

---

## Schema Loading Codes

These codes handle schema file access and parsing errors.

| Code | Module | Severity | Description |
|------|--------|----------|-------------|
| `SCHEMA_FILE_MISSING` | loader | HALT | Schema file not found |
| `SCHEMA_INVALID_JSON` | loader | HALT | Schema file is not valid JSON |

---

## Warning Codes (Non-Fatal)

These codes identify potential issues that do not prevent pack loading but should be addressed.

| Code | Module | Severity | Description |
|------|--------|----------|-------------|
| `DEPRECATED_DEFINITION` | validator | WARN | Definition is marked as deprecated |
| `EMPTY_REFERENCES` | validator | WARN | Definition has empty references list |
| `MISSING_DESCRIPTION` | validator | WARN | Definition missing description field |

**Note:** Warning codes are advisory only. They do not cause validation failure but may be logged for review.

---

## Modification Guidelines

**Changes Allowed Without RFC:**
- ✅ Adding new infrastructure codes (document here)
- ✅ Improving error message clarity
- ✅ Adding optional context fields
- ✅ Adjusting severity (WARN ↔ HALT for non-spine codes only)

**Changes Requiring Review:**
- ⚠️ Removing codes (may break existing monitoring)
- ⚠️ Changing code names (impacts log parsing)

**Forbidden:**
- ❌ Moving spine validation codes here (they belong in ERROR_CODES_LOCKED.md)
- ❌ Weakening spine validation (all spine codes remain HALT)

---

## Implementation Notes

**Code Location:** `core/packs/errors.py`  
**Message Registry:** `ERROR_MESSAGES` dict in `core/packs/errors.py`  
**Test Coverage:** Infrastructure codes should have unit tests but are not subject to golden anti-regression tests

---

## Related Documentation

- [ERROR_CODES_LOCKED.md](ERROR_CODES_LOCKED.md) — Governance-locked spine validation codes
- `core/packs/errors.py` — Canonical implementation
- `core/packs/loader.py` — Pack loading implementation

---

**END OF INFRASTRUCTURE ERROR CODES**
