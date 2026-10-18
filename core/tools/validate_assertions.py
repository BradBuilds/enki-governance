"""
Assertion Instance Validation CLI

Usage:
    python -m core.tools.validate_assertions <path_to_assertions.json>

Validates assertion instances against Identity Pack and Assertion Vocabulary Pack.
Exit codes:
    0 = all assertions valid
    1 = validation errors found
"""

import json
import sys
from pathlib import Path

from core.packs import load_pack
from core.validation.validate_assertions import validate_assertion_batch


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m core.tools.validate_assertions <path_to_assertions.json>")
        sys.exit(1)
    
    assertions_file = Path(sys.argv[1])
    
    if not assertions_file.exists():
        print(f"Error: File not found: {assertions_file}")
        sys.exit(1)
    
    print(f"Validating assertions: {assertions_file}")
    print("-" * 60)
    
    # Load vocabulary packs
    print("Loading Identity Pack...")
    identity_pack = load_pack("libraries/identity")
    if not identity_pack.ok:
        print("✗ Failed to load Identity Pack:")
        for error in identity_pack.errors:
            print(f"  {error.code}: {error.message}")
        sys.exit(1)
    print(f"✓ Identity Pack loaded (version {identity_pack.version})")
    
    print("Loading Assertion Vocabulary Pack...")
    assertion_vocab_pack = load_pack("libraries/assertion_vocab")
    if not assertion_vocab_pack.ok:
        print("✗ Failed to load Assertion Vocabulary Pack:")
        for error in assertion_vocab_pack.errors:
            print(f"  {error.code}: {error.message}")
        sys.exit(1)
    print(f"✓ Assertion Vocabulary Pack loaded (version {assertion_vocab_pack.version})")
    
    # Load assertions
    print(f"\nLoading assertions from {assertions_file}...")
    try:
        with open(assertions_file, 'r') as f:
            data = json.load(f)
        
        # Support both single assertion and list
        if isinstance(data, dict):
            assertions = [data]
        elif isinstance(data, list):
            assertions = data
        else:
            print(f"✗ Invalid format: expected object or array")
            sys.exit(1)
        
        print(f"✓ Loaded {len(assertions)} assertion(s)")
    except json.JSONDecodeError as e:
        print(f"✗ JSON parse error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error reading file: {e}")
        sys.exit(1)
    
    # Validate
    print("\nValidating assertions...")
    result = validate_assertion_batch(assertions, identity_pack, assertion_vocab_pack)
    
    print("-" * 60)
    
    if result.valid:
        print("✅ ALL ASSERTIONS VALID")
        print(f"\nValidated {len(assertions)} assertion(s) successfully.")
        sys.exit(0)
    else:
        print("❌ VALIDATION ERRORS FOUND")
        print(f"\nErrors ({len(result.errors)}):")
        for error in result.errors:
            if error.field:
                print(f"  [{error.code}] {error.field}: {error.message}")
            else:
                print(f"  [{error.code}] {error.message}")
        sys.exit(1)


if __name__ == "__main__":
    main()
