# gdu.numeric.core (LOCKED v1.0.0)

**Pack ID**: `gdu.numeric.core`  
**Status**: 🔒 LOCKED  
**Version**: 1.0.0  
**Authority**: GDU (sole-maintainer governance)  
**Lock Date**: 2026-01-13

---

## Purpose

This pack defines **canonical numeric truth** for ENKI.

It establishes the fundamental rule: **Decimal is canonical truth; Base-60 is representation only.**

---

## Canonical Rule

- **Decimal** is canonical truth
- **Base-60** is a representational layer only

This separation is **non-negotiable** and enforced via:
- Pack architecture (numeric truth separate from glyphs)
- Test invariants (no glyph leakage tests)
- Definition structure (explicit `role` fields)

---

## What's Included

### Core Definitions

1. **numeric.system.json** - System-wide numeric rules
   - Decimal as canonical base
   - Base-60 as representation layer
   - Hard rules preventing contamination

2. **numeric.decimal.json** - Decimal specification
   - Role: `canonical`
   - Domain: Non-negative integers (v1.0.0)
   - Storage: All GDU numeric values stored as decimal

3. **numeric.base60.json** - Base-60 specification
   - Role: `representation_only`
   - Digit range: 0-59
   - Zero rule: Explicit `[0]`, never omitted
   - Place order: Left-to-right, highest to lowest

4. **numeric.place_value.json** - Place value structure
   - Definition of place sequences
   - Examples: `[0]`→0, `[59]`→59, `[1,0]`→60, `[1,2,3]`→3723
   - Immutability: Preserve sequences exactly as entered

5. **numeric.conversion_rules.json** - Conversion invariants
   - Decimal ↔ Base-60 algorithms (declarative)
   - Lossless invariant: Round-trip must preserve value
   - Rejection rules for invalid inputs

---

## Examples

### Canonical Storage (Decimal)

```json
{
  "value": 3723,
  "canonical_base": 10
}
```

✅ **Correct**: Truth stored as decimal integer

### Representation (Base-60)

```json
{
  "places": [1, 2, 3],
  "represents_decimal": 3723
}
```

✅ **Correct**: Base-60 as representation of decimal truth

### What NOT to Do

```json
{
  "value": [1, 2, 3],
  "canonical_base": 60
}
```

❌ **Wrong**: Base-60 cannot be canonical truth

```json
{
  "value": 3723,
  "glyphs": ["𒐕", "𒐖", "𒐗"]
}
```

❌ **Wrong**: Glyphs belong in separate pack, not numeric core

---

## Explicit Non-Goals

This pack **does NOT include**:

- ❌ Glyph rendering
- ❌ Unicode symbols
- ❌ Language interpretation
- ❌ Culture/scoping semantics
- ❌ UI display logic
- ❌ Negative numbers (v1.0.0)
- ❌ Floating point (v1.0.0)
- ❌ Rational numbers (v1.0.0)

---

## Dependencies

### What This Pack Depends On

**None** - This is a foundational pack (Layer A).

### What Depends On This Pack

- **gdu.glyphs.base60** (planned) - Glyph rendering for base-60 digits
- Future packs that need numeric representations

Glyph rendering is defined in a **separate pack** that depends on this pack:
- Pack: `gdu.glyphs.base60` (next)
- Dependency: `gdu.numeric.core` ^1.0.0

This ensures **glyphs can never contaminate numeric truth**.

---

## Why This Exists

### The Problem

Mixing appearance (glyphs) with truth (numbers) causes:
- Ambiguity about which representation is authoritative
- Contamination of semantic meaning by rendering concerns
- Difficulty verifying correctness
- Context loss across build sessions

### The Solution

**Separate truth from appearance:**

| Layer | Pack | Contents |
|-------|------|----------|
| Truth | `gdu.numeric.core` | Decimal, Base-60 places (no rendering) |
| Appearance | `gdu.glyphs.base60` | Glyph symbols, rendering rules |

**Dependency direction**: Appearance depends on truth, never vice versa.

---

## Invariants

See [gdu.numeric.core.invariants.md](../../docs/specs/gdu.numeric.core.invariants.md) for formal invariants.

Quick summary:
- **INV-001**: Decimal is canonical truth
- **INV-002**: Base-60 is representational only
- **INV-003**: No rendering system may affect numeric truth
- **INV-004**: All conversions must be lossless and deterministic
- **SEP-001**: No glyph tokens in numeric core

---

## Testing

### Test Suite

Location: `tests/test_numeric_core.py`

### Coverage

- Manifest integrity
- Definition file validation
- Forbidden glyph leakage
- Conversion rule presence
- Invariant mapping

### Run Tests

```bash
cd <path> Node
pytest libraries/numeric/tests/test_numeric_core.py -v
```

---

## Governance

### Authority

- **Owner**: Enki Systems (sole maintainer)
- **Change Control**: SOLE_MAINTAINER
- **Admission**: CEREMONY_REQUIRED

### Immutability

This pack is **LOCKED** as of 2026-01-13.

Semantic meaning cannot change in v1.0.0.

### Future Versions

To extend this pack:
- ✅ Create new version (v2.0.0) for breaking changes
- ✅ Create new dependent pack (e.g., `gdu.numeric.signed`) for extensions
- ❌ Do NOT modify v1.0.0 definitions

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0.0 | 2026-01-13 | Initial lock - unsigned integers, decimal canonical, base-60 representational |

---

## Related Documentation

- **Master Plan**: [GDU_MASTER_PLAN.md](../../docs/GDU_MASTER_PLAN.md)
- **Governance**: [00-gdu-master-plan-supplement.md](../../docs/00-gdu-master-plan-supplement.md)
- **Pack Index**: [GDU_PACK_INDEX.md](../../docs/GDU_PACK_INDEX.md)
- **Test Matrix**: [GDU_TEST_MATRIX.md](../../docs/GDU_TEST_MATRIX.md)
- **Invariants**: [gdu.numeric.core.invariants.md](../../docs/specs/gdu.numeric.core.invariants.md)

---

## Quick Reference

### Decimal → Base-60

```python
# Conceptual (see conversion_rules.json for formal definition)
def decimal_to_base60(value: int) -> list[int]:
    if value == 0:
        return [0]
    places = []
    while value > 0:
        places.append(value % 60)
        value //= 60
    return list(reversed(places))
```

### Base-60 → Decimal

```python
# Conceptual (see conversion_rules.json for formal definition)
def base60_to_decimal(places: list[int]) -> int:
    result = 0
    for i, digit in enumerate(places):
        result += digit * (60 ** (len(places) - 1 - i))
    return result
```

### Lossless Invariant

```python
assert base60_to_decimal(decimal_to_base60(3723)) == 3723
```

---

**End of README**
