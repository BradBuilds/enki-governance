#!/usr/bin/env python3
"""
ENKI Root Key Generation CLI Tool (Phase 3.6 Milestone 1).

Generate and display ENKI Ed25519 root keypair for device authentication.
Hardware-independent mode (no HSM/TPM integration).

Usage:
    python core/tools/generate_root_key.py [--deterministic SEED_HEX]

Options:
    --deterministic SEED_HEX    Use deterministic seed (64 hex chars = 32 bytes)
                                For testing/reproducibility only. NOT FOR PRODUCTION.

Examples:
    # Generate random key (production)
    python core/tools/generate_root_key.py

    # Generate deterministic key (testing)
    python core/tools/generate_root_key.py --deterministic 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
"""

import sys
import argparse
from pathlib import Path

# Add workspace root to Python path
workspace_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(workspace_root))

from storage.auth.root_key import (
    generate_root_key,
    export_public_key_bytes,
    export_private_key_bytes,
)


def main():
    parser = argparse.ArgumentParser(
        description="Generate ENKI Ed25519 root keypair for device authentication"
    )
    parser.add_argument(
        "--deterministic",
        type=str,
        help="32-byte seed as hex string (64 hex chars) for deterministic generation (TESTING ONLY)",
    )
    
    args = parser.parse_args()
    
    # Generate keypair
    seed = None
    if args.deterministic:
        try:
            seed = bytes.fromhex(args.deterministic)
            if len(seed) != 32:
                print(f"ERROR: Seed must be exactly 32 bytes (64 hex chars), got {len(seed)} bytes", file=sys.stderr)
                sys.exit(1)
            print("⚠️  DETERMINISTIC MODE: Using provided seed (NOT FOR PRODUCTION)", file=sys.stderr)
        except ValueError as e:
            print(f"ERROR: Invalid hex string: {e}", file=sys.stderr)
            sys.exit(1)
    
    private_key, public_key = generate_root_key(seed=seed)
    
    # Export as raw bytes
    private_bytes = export_private_key_bytes(private_key)
    public_bytes = export_public_key_bytes(public_key)
    
    # Display results
    print("=" * 80)
    print("ENKI ROOT KEY PAIR (Ed25519)")
    print("=" * 80)
    print()
    print("Private Key (32 bytes):")
    print(private_bytes.hex())
    print()
    print("Public Key (32 bytes):")
    print(public_bytes.hex())
    print()
    print("=" * 80)
    
    if seed is None:
        print("✅ Generated cryptographically secure random keypair")
        print("⚠️  Store private key securely! It cannot be recovered if lost.")
    else:
        print("⚠️  DETERMINISTIC KEYPAIR - FOR TESTING ONLY")
        print("    DO NOT use deterministic keys in production!")
    print("=" * 80)


if __name__ == "__main__":
    main()
