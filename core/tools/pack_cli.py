#!/usr/bin/env python3
"""
ENKI Pack CLI — Python-native pack management tool.

Replaces the Go gdu CLI. All pack operations in one language,
same hash algorithm as the spine (SHA-256 + JCS + Unicode NFC).

Usage:
    python -m core.tools.pack_cli init <pack_id> [--name "..."] [--namespace ...]
    python -m core.tools.pack_cli add <pack_id> <definition_id> [--type system]
    python -m core.tools.pack_cli digest <pack_id>
    python -m core.tools.pack_cli validate <pack_id>
    python -m core.tools.pack_cli list
"""

import sys
import json
import re
import argparse
from pathlib import Path
from datetime import datetime, timezone

# Project root resolution
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.spine.content_hash import compute_pack_hash, validate_hash_format

# Constants
LIBRARIES_DIR = PROJECT_ROOT / "libraries"
PACK_ID_PATTERN = re.compile(r'^[a-z][a-z0-9]*(?:\.[a-z][a-z0-9]*)*$')
DEFINITION_ID_PATTERN = re.compile(r'^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$')
SEMVER_PATTERN = re.compile(r'^\d+\.\d+\.\d+$')

# Volatile field patterns (LOCKED — from Gate A)
VOLATILE_PATTERNS = [
    r'/tmp/', r'C:\\Users\\', r'/home/[^/]+/',
    r'hostname', r'process_id', r'pid:',
]


def cmd_init(args):
    """Create a new pack directory with pack.json and definitions/ folder."""
    pack_id = args.pack_id

    if not PACK_ID_PATTERN.match(pack_id):
        print(f"Error: Invalid pack_id '{pack_id}'")
        print("  Must be lowercase, dot-separated: signal, medical.patient, etc.")
        sys.exit(1)

    pack_dir = LIBRARIES_DIR / pack_id
    if pack_dir.exists():
        print(f"Error: Pack already exists at {pack_dir}")
        sys.exit(1)

    name = args.name or f"ENKI {pack_id.replace('.', ' ').title()} Pack"
    namespaces = args.namespace or [pack_id]
    version = args.version or "0.1.0"

    # Create directories
    definitions_dir = pack_dir / "definitions"
    examples_dir = pack_dir / "examples"
    definitions_dir.mkdir(parents=True)
    examples_dir.mkdir(parents=True)

    # Create system definition (every pack gets one)
    system_def = {
        "id": f"{pack_id}.system",
        "version": version,
        "system": {
            "name": name,
            "description": f"System definition for {pack_id} domain"
        }
    }
    system_path = definitions_dir / f"{pack_id}.system.json"
    with open(system_path, 'w', encoding='utf-8') as f:
        json.dump(system_def, f, indent=2, ensure_ascii=False)

    # Create pack.json manifest
    manifest = {
        "pack_id": pack_id,
        "version": version,
        "name": name,
        "description": f"Domain pack for {pack_id}",
        "definitions": [
            {
                "id": f"{pack_id}.system",
                "path": f"definitions/{pack_id}.system.json",
                "type": "system"
            }
        ],
        "dependencies": [],
        "namespaces_owned": namespaces,
        "metadata": {
            "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "author": "ENKI System",
            "license": "Proprietary"
        }
    }

    with open(pack_dir / "pack.json", 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Pack created: {pack_dir}")
    print(f"  pack_id:    {pack_id}")
    print(f"  version:    {version}")
    print(f"  namespace:  {', '.join(namespaces)}")
    print(f"  manifest:   {pack_dir / 'pack.json'}")
    print(f"  system def: {system_path}")
    print()
    print("Next steps:")
    print(f"  python -m core.tools.pack_cli add {pack_id} {pack_id}.types --type entities")
    print(f"  python -m core.tools.pack_cli digest {pack_id}")
    print(f"  python -m core.tools.pack_cli validate {pack_id}")


def cmd_add(args):
    """Add a new definition file to an existing pack."""
    pack_id = args.pack_id
    definition_id = args.definition_id
    def_type = args.type or "entities"

    pack_dir = LIBRARIES_DIR / pack_id
    if not pack_dir.exists():
        print(f"Error: Pack not found at {pack_dir}")
        print(f"  Run: python -m core.tools.pack_cli init {pack_id}")
        sys.exit(1)

    if not DEFINITION_ID_PATTERN.match(definition_id):
        print(f"Error: Invalid definition_id '{definition_id}'")
        sys.exit(1)

    # Check namespace ownership
    manifest_path = pack_dir / "pack.json"
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    namespaces = manifest.get("namespaces_owned", [])
    owned = any(definition_id == ns or definition_id.startswith(ns + ".") for ns in namespaces)
    if not owned:
        print(f"Error: Definition '{definition_id}' not under owned namespaces: {namespaces}")
        print(f"  Definition IDs must start with an owned namespace prefix.")
        sys.exit(1)

    # Check for duplicate
    existing_ids = [d["id"] for d in manifest.get("definitions", [])]
    if definition_id in existing_ids:
        print(f"Error: Definition '{definition_id}' already exists in pack manifest")
        sys.exit(1)

    # Create definition file
    def_filename = f"{definition_id}.json"
    def_path = pack_dir / "definitions" / def_filename

    if def_path.exists():
        print(f"Error: Definition file already exists: {def_path}")
        sys.exit(1)

    definition = {
        "id": definition_id,
        "version": manifest["version"],
        "terms": [
            {
                "term": "placeholder",
                "definition": "Replace this with your actual definition",
                "category": def_type
            }
        ]
    }

    with open(def_path, 'w', encoding='utf-8') as f:
        json.dump(definition, f, indent=2, ensure_ascii=False)

    # Update manifest
    manifest["definitions"].append({
        "id": definition_id,
        "path": f"definitions/{def_filename}",
        "type": def_type
    })

    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"Definition added: {definition_id}")
    print(f"  file: {def_path}")
    print(f"  type: {def_type}")
    print(f"  Edit the file to add your actual terms/definitions.")


def cmd_digest(args):
    """Compute and display the content hash for a pack."""
    pack_id = args.pack_id
    pack_dir = LIBRARIES_DIR / pack_id

    if not pack_dir.exists():
        print(f"Error: Pack not found at {pack_dir}")
        sys.exit(1)

    try:
        content_hash = compute_pack_hash(str(pack_dir))
        print(f"{pack_id}: {content_hash}")
        if args.verify:
            manifest_path = pack_dir / "pack.json"
            # Check if there's a declared hash to verify against
            print(f"  Format valid: {validate_hash_format(content_hash)}")
    except Exception as e:
        print(f"Error computing hash for {pack_id}: {e}")
        sys.exit(1)


def cmd_validate(args):
    """Run full validation on a pack (structure, schema, volatiles, namespaces)."""
    pack_id = args.pack_id
    pack_dir = LIBRARIES_DIR / pack_id
    errors = []

    if not pack_dir.exists():
        print(f"Error: Pack not found at {pack_dir}")
        sys.exit(1)

    # 1. Check pack.json exists and is valid
    manifest_path = pack_dir / "pack.json"
    if not manifest_path.exists():
        errors.append("pack.json missing")
        _report(pack_id, errors)
        return

    try:
        with open(manifest_path, 'r', encoding='utf-8') as f:
            manifest = json.load(f)
    except json.JSONDecodeError as e:
        errors.append(f"pack.json invalid JSON: {e}")
        _report(pack_id, errors)
        return

    # 2. Check required fields
    for field in ["pack_id", "version", "name", "definitions"]:
        if field not in manifest:
            errors.append(f"Missing required field: {field}")

    if errors:
        _report(pack_id, errors)
        return

    # 3. Validate patterns
    if not PACK_ID_PATTERN.match(manifest["pack_id"]):
        errors.append(f"Invalid pack_id pattern: {manifest['pack_id']}")

    if not SEMVER_PATTERN.match(manifest["version"]):
        errors.append(f"Invalid version (must be semver): {manifest['version']}")

    if manifest["pack_id"] != pack_id:
        errors.append(f"pack_id mismatch: directory={pack_id}, manifest={manifest['pack_id']}")

    # 4. Check definitions exist
    definitions = manifest.get("definitions", [])
    if len(definitions) == 0:
        errors.append("No definitions declared (minimum 1)")

    for defn in definitions:
        if "id" not in defn or "path" not in defn:
            errors.append(f"Definition entry missing id or path: {defn}")
            continue

        def_path = pack_dir / defn["path"]
        if not def_path.exists():
            errors.append(f"Definition file missing: {defn['path']}")
        else:
            # Validate it's valid JSON
            try:
                with open(def_path, 'r', encoding='utf-8') as f:
                    json.load(f)
            except json.JSONDecodeError as e:
                errors.append(f"Definition {defn['id']} invalid JSON: {e}")

    # 5. Check namespaces declared
    namespaces = manifest.get("namespaces_owned", [])
    if not namespaces:
        errors.append("No namespaces_owned declared (required for admission)")

    for ns in namespaces:
        if not PACK_ID_PATTERN.match(ns):
            errors.append(f"Invalid namespace format: {ns}")

    # 6. Check namespace ownership of definitions
    for defn in definitions:
        def_id = defn.get("id", "")
        owned = any(def_id == ns or def_id.startswith(ns + ".") for ns in namespaces)
        if not owned:
            errors.append(f"Definition '{def_id}' not under owned namespaces: {namespaces}")

    # 7. Scan for volatile fields (Gate A)
    raw_content = manifest_path.read_text(encoding='utf-8')
    for pattern in VOLATILE_PATTERNS:
        if re.search(pattern, raw_content):
            errors.append(f"Volatile content detected in pack.json: pattern '{pattern}'")

    # 8. Compute hash
    try:
        content_hash = compute_pack_hash(str(pack_dir))
        hash_valid = True
    except Exception as e:
        errors.append(f"Hash computation failed: {e}")
        hash_valid = False
        content_hash = None

    # Report
    _report(pack_id, errors, content_hash)


def _report(pack_id, errors, content_hash=None):
    """Print validation report."""
    if errors:
        print(f"FAIL  {pack_id}")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print(f"PASS  {pack_id}")
        print(f"  definitions: OK")
        print(f"  namespaces:  OK")
        print(f"  volatiles:   clean")
        if content_hash:
            print(f"  hash:        {content_hash}")


def cmd_list(args):
    """List all packs in libraries/ with their status."""
    if not LIBRARIES_DIR.exists():
        print("No libraries/ directory found")
        sys.exit(1)

    packs = sorted([
        d.name for d in LIBRARIES_DIR.iterdir()
        if d.is_dir() and (d / "pack.json").exists()
    ])

    if not packs:
        print("No packs found in libraries/")
        return

    print(f"{'PACK ID':<25} {'VERSION':<10} {'DEFS':<6} {'NAMESPACES':<30} {'HASH'}")
    print("-" * 110)

    for pack_id in packs:
        pack_dir = LIBRARIES_DIR / pack_id
        try:
            with open(pack_dir / "pack.json", 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            version = manifest.get("version", "?")
            defs = len(manifest.get("definitions", []))
            ns = ", ".join(manifest.get("namespaces_owned", []))
            content_hash = compute_pack_hash(str(pack_dir))
            short_hash = content_hash[:20] + "..."
            print(f"{pack_id:<25} {version:<10} {defs:<6} {ns:<30} {short_hash}")
        except Exception as e:
            print(f"{pack_id:<25} ERROR: {e}")


def cmd_active_set(args):
    """Generate a new active_set JSON from current library packs."""
    if not LIBRARIES_DIR.exists():
        print("No libraries/ directory found")
        sys.exit(1)

    # Find all valid packs
    packs = []
    for pack_dir in sorted(LIBRARIES_DIR.iterdir()):
        manifest_path = pack_dir / "pack.json"
        if not pack_dir.is_dir() or not manifest_path.exists():
            continue

        try:
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            content_hash = compute_pack_hash(str(pack_dir))
            packs.append({
                "pack_id": manifest["pack_id"],
                "version": manifest["version"],
                "content_hash": content_hash
            })
        except Exception as e:
            print(f"Warning: Skipping {pack_dir.name}: {e}")

    # Determine version
    version = args.version
    if not version:
        # Auto-increment: find latest active_set_v*.json
        existing = sorted(PROJECT_ROOT.glob("active_set_v*.json"))
        if existing:
            last = existing[-1].stem  # e.g. "active_set_v3"
            try:
                last_num = int(last.split("_v")[1])
                version = f"{last_num + 1}.0.0"
            except (IndexError, ValueError):
                version = "1.0.0"
        else:
            version = "1.0.0"

    active_set = {
        "version": version,
        "packs": packs,
        "metadata": {
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generator": "core.tools.pack_cli",
            "pack_count": len(packs)
        }
    }

    # Determine output filename
    major = version.split(".")[0]
    output_path = args.output or PROJECT_ROOT / f"active_set_v{major}.json"

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(active_set, f, indent=2, ensure_ascii=False)

    print(f"Active set generated: {output_path}")
    print(f"  version: {version}")
    print(f"  packs:   {len(packs)}")
    for p in packs:
        print(f"    - {p['pack_id']} v{p['version']} [{p['content_hash'][:20]}...]")


def main():
    parser = argparse.ArgumentParser(
        prog="pack",
        description="ENKI Pack CLI — create, validate, and manage governance packs"
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # init
    p_init = subparsers.add_parser("init", help="Create a new pack")
    p_init.add_argument("pack_id", help="Pack identifier (lowercase, dot-separated)")
    p_init.add_argument("--name", help="Human-readable pack name")
    p_init.add_argument("--namespace", nargs="+", help="Namespace prefixes to own")
    p_init.add_argument("--version", default="0.1.0", help="Initial version")

    # add
    p_add = subparsers.add_parser("add", help="Add a definition to a pack")
    p_add.add_argument("pack_id", help="Target pack")
    p_add.add_argument("definition_id", help="Definition identifier")
    p_add.add_argument("--type", default="entities", help="Definition type")

    # digest
    p_digest = subparsers.add_parser("digest", help="Compute pack content hash")
    p_digest.add_argument("pack_id", help="Pack to hash")
    p_digest.add_argument("--verify", action="store_true", help="Verify hash format")

    # validate
    p_validate = subparsers.add_parser("validate", help="Validate pack structure")
    p_validate.add_argument("pack_id", help="Pack to validate")

    # list
    subparsers.add_parser("list", help="List all packs")

    # active-set
    p_as = subparsers.add_parser("active-set", help="Generate active set from current packs")
    p_as.add_argument("--version", help="Active set version (auto-increments if omitted)")
    p_as.add_argument("--output", type=Path, help="Output file path")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "digest":
        cmd_digest(args)
    elif args.command == "validate":
        cmd_validate(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "active-set":
        cmd_active_set(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
