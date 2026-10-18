"""
Audit Event Instance Validation CLI

Usage:
    python -m core.tools.validate_audit_events <path_to_audit_events.json>

Validates audit event instances against Audit Vocabulary Pack and Identity Pack.
Exit codes:
    0 = all events valid
    1 = validation errors found
"""

import json
import sys
from pathlib import Path

from core.packs import load_pack
from core.validation.validate_audit_events import validate_audit_event_batch


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m core.tools.validate_audit_events <path_to_audit_events.json>")
        sys.exit(1)
    
    events_file = Path(sys.argv[1])
    
    if not events_file.exists():
        print(f"Error: File not found: {events_file}")
        sys.exit(1)
    
    print(f"Validating audit events: {events_file}")
    print("-" * 60)
    
    # Load vocabulary packs
    print("Loading Audit Vocabulary Pack...")
    audit_vocab_pack = load_pack("libraries/audit_vocab")
    if not audit_vocab_pack.ok:
        print("✗ Failed to load Audit Vocabulary Pack:")
        for error in audit_vocab_pack.errors:
            print(f"  {error.code}: {error.message}")
        sys.exit(1)
    print(f"✓ Audit Vocabulary Pack loaded (version {audit_vocab_pack.version})")
    
    print("Loading Identity Pack...")
    identity_pack = load_pack("libraries/identity")
    if not identity_pack.ok:
        print("✗ Failed to load Identity Pack:")
        for error in identity_pack.errors:
            print(f"  {error.code}: {error.message}")
        sys.exit(1)
    print(f"✓ Identity Pack loaded (version {identity_pack.version})")
    
    # Load events
    print(f"\nLoading audit events from {events_file}...")
    try:
        with open(events_file, 'r') as f:
            data = json.load(f)
        
        # Support both single event and list
        if isinstance(data, dict):
            events = [data]
        elif isinstance(data, list):
            events = data
        else:
            print(f"✗ Invalid format: expected object or array")
            sys.exit(1)
        
        print(f"✓ Loaded {len(events)} audit event(s)")
    except json.JSONDecodeError as e:
        print(f"✗ JSON parse error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Error reading file: {e}")
        sys.exit(1)
    
    # Validate
    print("\nValidating audit events...")
    result = validate_audit_event_batch(events, audit_vocab_pack, identity_pack)
    
    print("-" * 60)
    
    if result.valid:
        print("✅ ALL AUDIT EVENTS VALID")
        print(f"\nValidated {len(events)} audit event(s) successfully.")
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
