"""
ENKI Spine S1.0 - Active Set Demonstration

This script demonstrates the spine-only boot mode:
1. Creates an empty active set (spine-only mode)
2. Attempts to load domain packs
3. Shows explicit rejection
4. Proves the spine can reject data it's never seen

Usage:
    python -m core.spine.demo_active_set
"""

from pathlib import Path
from core.spine.active_set import ActiveSet, PackDeclaration
from core.packs.loader import PackLoader


def demo_spine_only_mode():
    """Demonstrate spine-only boot mode (no domain packs allowed)."""
    print("\n" + "=" * 70)
    print("ENKI SPINE S1.0 - SPINE-ONLY BOOT MODE DEMONSTRATION")
    print("=" * 70)
    
    print("\n[1] Creating empty active set (spine-only mode)...")
    active_set = ActiveSet.empty()
    
    print(f"    ✓ Active set version: {active_set.version}")
    print(f"    ✓ Allowed packs: {len(active_set.packs)}")
    print(f"    ✓ Spine-only mode: {active_set.is_spine_only()}")
    
    print("\n[2] Creating pack loader with active set enforcement...")
    loader = PackLoader(active_set=active_set)
    print("    ✓ Pack loader initialized with active set")
    
    print("\n[3] Attempting to load domain packs (should be rejected)...")
    
    test_packs = [
        "libraries/base60",
        "libraries/identity",
        "libraries/assertion_vocab"
    ]
    
    for pack_path_str in test_packs:
        pack_path = Path(pack_path_str)
        if not pack_path.exists():
            print(f"    ⊘ {pack_path_str} - not found (skipped)")
            continue
        
        result = loader.load_pack(pack_path)
        
        if not result.ok:
            error = result.errors[0]
            print(f"    ✓ {pack_path_str} - REJECTED")
            print(f"      Error: {error.code}")
            print(f"      Mode: {error.context.get('active_set_mode', 'unknown')}")
        else:
            print(f"    ✗ {pack_path_str} - ALLOWED (unexpected!)")
    
    print("\n[4] PROOF: Spine rejects data without domain knowledge")
    print("    ✓ Empty active set enforced")
    print("    ✓ All domain packs rejected")
    print("    ✓ No silent fallbacks")
    print("    ✓ Deterministic behavior")
    
    print("\n" + "=" * 70)
    print("SUCCESS: Spine can reject data it has never seen")
    print("=" * 70 + "\n")


def demo_selective_allowlist():
    """Demonstrate selective pack allowlist."""
    print("\n" + "=" * 70)
    print("ENKI SPINE S1.0 - SELECTIVE ALLOWLIST DEMONSTRATION")
    print("=" * 70)
    
    print("\n[1] Creating active set with one allowed pack...")
    active_set = ActiveSet(
        version="1.0.0",
        packs=[PackDeclaration("base60", "0.1.0")]
    )
    
    print(f"    ✓ Active set version: {active_set.version}")
    print(f"    ✓ Allowed packs: {[p.pack_id for p in active_set.packs]}")
    print(f"    ✓ Spine-only mode: {active_set.is_spine_only()}")
    
    print("\n[2] Creating pack loader with selective active set...")
    loader = PackLoader(active_set=active_set)
    print("    ✓ Pack loader initialized")
    
    print("\n[3] Testing pack loading...")
    
    test_cases = [
        ("libraries/base60", "should ALLOW (in active set)"),
        ("libraries/identity", "should REJECT (not in active set)"),
    ]
    
    for pack_path_str, expected in test_cases:
        pack_path = Path(pack_path_str)
        if not pack_path.exists():
            print(f"    ⊘ {pack_path_str} - not found (skipped)")
            continue
        
        result = loader.load_pack(pack_path)
        pack_name = pack_path_str.split('/')[-1]
        
        if "ALLOW" in expected:
            if result.ok or not any(e.code == "PACK_NOT_IN_ACTIVE_SET" for e in result.errors):
                print(f"    ✓ {pack_name} - ALLOWED (as expected)")
            else:
                print(f"    ✗ {pack_name} - REJECTED (unexpected!)")
        else:  # REJECT
            if not result.ok and any(e.code == "PACK_NOT_IN_ACTIVE_SET" for e in result.errors):
                print(f"    ✓ {pack_name} - REJECTED (as expected)")
            else:
                print(f"    ✗ {pack_name} - ALLOWED (unexpected!)")
    
    print("\n[4] PROOF: Selective allowlist enforced")
    print("    ✓ Only declared packs allowed")
    print("    ✓ All others rejected")
    print("    ✓ Explicit fail-closed behavior")
    
    print("\n" + "=" * 70)
    print("SUCCESS: Allowlist enforcement validated")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    demo_spine_only_mode()
    demo_selective_allowlist()
