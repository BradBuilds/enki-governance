#!/usr/bin/env python3
"""
Admission Ceremony Orchestrator

Runs the full S1 spine validation ceremony for a pack set and produces
deterministic artifacts: ValidationTranscript + ActivationRecord.

This script is a thin orchestration layer over S1 locked modules.
It does NOT modify any S1 artifacts.

Usage:
    python scripts/governance/run_admission_ceremony.py active_set_v1.json
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
import uuid

# Add project root to path
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

from core.spine.active_set import load_active_set
from core.spine.content_hash import compute_pack_hash, verify_pack_hash
from core.spine import namespace_registry
from core.spine.validation_transcript import ValidationTranscript, TranscriptEntry, ValidationOutcome
from core.spine.activation_gate import ActivationRecord, create_activation_record, save_activation_record


# Pack location resolver — auto-discovers from libraries/ directory
# Canonical location: project_root/libraries/<pack_id>
LIBRARIES_DIR = project_root / "libraries"


def resolve_pack_location(pack_id: str) -> Path:
    """Resolve pack_id to filesystem location."""
    pack_path = LIBRARIES_DIR / pack_id
    if not pack_path.exists() or not (pack_path / "pack.json").exists():
        raise ValueError(f"Pack not found: {pack_id} (expected at {pack_path})")
    return pack_path


def run_ceremony(active_set_path: Path, output_dir: Path, operator: str) -> dict:
    """
    Run the full admission ceremony and produce artifacts.
    
    Returns:
        dict with ceremony results and artifact paths
    """
    print(f"\n{'='*70}")
    print(f"🎭 ADMISSION CEREMONY STARTING")
    print(f"{'='*70}\n")
    
    # Initialize transcript
    transcript = ValidationTranscript()
    results = {
        "success": False,
        "active_set_path": str(active_set_path),
        "artifacts": {}
    }
    
    # Step 1: Load active set
    print("📋 Step 1: Loading active set...")
    try:
        active_set = load_active_set(active_set_path)  # Schema validation is automatic
        transcript.record_step(
            step_type="active_set_load",
            outcome=ValidationOutcome.PASS,
            pack_id="_system",
            context={"version": active_set.version, "pack_count": len(active_set.packs)}
        )
        print(f"   ✅ Loaded: v{active_set.version}")
        print(f"   📦 Packs: {len(active_set.packs)}")
    except Exception as e:
        transcript.record_step(
            step_type="active_set_load",
            outcome=ValidationOutcome.FAIL,
            pack_id="_system",
            error_code="ACTIVE_SET_LOAD_FAILED",
            context={"error": str(e)}
        )
        print(f"   ❌ Failed: {e}")
        return results
    
    # Step 2: Verify content hashes
    print("\n🔐 Step 2: Verifying pack content hashes...")
    for pack in active_set.packs:
        pack_path = resolve_pack_location(pack.pack_id)
        try:
            is_valid = verify_pack_hash(pack_path, pack.content_hash)
            if is_valid:
                transcript.record_step(
                    step_type="content_hash_verification",
                    outcome=ValidationOutcome.PASS,
                    pack_id=pack.pack_id,
                    context={
                        "declared_hash": pack.content_hash,
                        "version": pack.version
                    }
                )
                print(f"   ✅ {pack.pack_id}: Hash verified")
            else:
                transcript.record_step(
                    step_type="content_hash_verification",
                    outcome=ValidationOutcome.FAIL,
                    pack_id=pack.pack_id,
                    error_code="PACK_HASH_MISMATCH",
                    context={
                        "declared_hash": pack.content_hash,
                        "version": pack.version
                    }
                )
                print(f"   ❌ {pack.pack_id}: Hash mismatch")
                return results
        except Exception as e:
            transcript.record_step(
                step_type="content_hash_verification",
                outcome=ValidationOutcome.FAIL,
                pack_id=pack.pack_id,
                error_code="HASH_VERIFICATION_ERROR",
                context={"error": str(e)}
            )
            print(f"   ❌ {pack.pack_id}: Error - {e}")
            return results
    
    # Step 3: Load packs and validate namespaces
    print("\n📚 Step 3: Loading packs and validating namespaces...")
    loaded_packs = []
    packs_namespaces = {}
    
    for pack_entry in active_set.packs:
        pack_path = resolve_pack_location(pack_entry.pack_id)
        try:
            # Load pack metadata
            pack_json_path = pack_path / "pack.json"
            with open(pack_json_path, 'r', encoding='utf-8') as f:
                pack_data = json.load(f)
            
            # Validate namespace declarations
            namespaces_owned = pack_data.get("namespaces_owned", [])
            namespace_errors = namespace_registry.validate_namespace_declarations(
                pack_id=pack_data["pack_id"],
                namespaces_owned=namespaces_owned
            )
            
            if namespace_errors:
                for error in namespace_errors:
                    transcript.record_step(
                        step_type="namespace_declaration_validation",
                        outcome=ValidationOutcome.FAIL,
                        pack_id=pack_data["pack_id"],
                        error_code=error.code,
                        context=error.context
                    )
                print(f"   ❌ {pack_data['pack_id']}: Invalid namespace declarations")
                return results
            
            transcript.record_step(
                step_type="namespace_declaration_validation",
                outcome=ValidationOutcome.PASS,
                pack_id=pack_data["pack_id"],
                context={
                    "namespaces_owned": namespaces_owned,
                    "definition_count": len(pack_data["definitions"])
                }
            )
            print(f"   ✅ {pack_data['pack_id']}: Namespace declarations valid")
            
            loaded_packs.append(pack_data)
            packs_namespaces[pack_data["pack_id"]] = namespaces_owned
            
        except Exception as e:
            transcript.record_step(
                step_type="pack_load",
                outcome=ValidationOutcome.FAIL,
                pack_id=pack_entry.pack_id,
                error_code="PACK_LOAD_FAILED",
                context={"error": str(e)}
            )
            print(f"   ❌ {pack_entry.pack_id}: Failed - {e}")
            return results
    
    # Step 4: Detect namespace collisions across packs
    print("\n🔍 Step 4: Detecting namespace collisions...")
    collision_errors = namespace_registry.detect_namespace_collisions(packs_namespaces)
    
    if collision_errors:
        for error in collision_errors:
            transcript.record_step(
                step_type="namespace_collision_detection",
                outcome=ValidationOutcome.FAIL,
                pack_id=error.context["pack_id"],
                error_code=error.code,
                context=error.context
            )
        print(f"   ❌ {len(collision_errors)} namespace collision(s) detected")
        return results
    
    transcript.record_step(
        step_type="namespace_collision_detection",
        outcome=ValidationOutcome.PASS,
        pack_id="_system",
        context={"pack_count": len(loaded_packs)}
    )
    print(f"   ✅ No collisions detected across {len(loaded_packs)} packs")
    
    # Step 5: Validate namespace ownership for all definitions
    print("\n🏷️  Step 5: Validating namespace ownership...")
    for pack in loaded_packs:
        try:
            ownership_errors = namespace_registry.verify_definition_ownership(
                pack_id=pack["pack_id"],
                namespaces_owned=pack.get("namespaces_owned", []),
                definition_ids=[d["id"] for d in pack["definitions"]]
            )
            
            if ownership_errors:
                for error in ownership_errors:
                    transcript.record_step(
                        step_type="namespace_ownership_validation",
                        outcome=ValidationOutcome.FAIL,
                        pack_id=pack["pack_id"],
                        error_code=error.code,
                        context=error.context
                    )
                print(f"   ❌ {pack['pack_id']}: {len(ownership_errors)} ownership violation(s)")
                return results
            
            transcript.record_step(
                step_type="namespace_ownership_validation",
                outcome=ValidationOutcome.PASS,
                pack_id=pack["pack_id"],
                context={"definition_count": len(pack["definitions"])}
            )
            print(f"   ✅ {pack['pack_id']}: All definitions owned")
                
        except Exception as e:
            transcript.record_step(
                step_type="namespace_ownership_validation",
                outcome=ValidationOutcome.FAIL,
                pack_id=pack["pack_id"],
                error_code="VALIDATION_ERROR",
                context={"error": str(e)}
            )
            print(f"   ❌ {pack['pack_id']}: Error - {e}")
            return results
    
    # Step 6: Compute transcript hash
    print("\n🧮 Step 6: Computing validation transcript hash...")
    transcript_hash = transcript.compute_hash()
    print(f"   ✅ Transcript hash: {transcript_hash}")
    
    # Step 7: Export transcript
    print("\n💾 Step 7: Exporting transcript...")
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = output_dir / "validation_transcript.json"
    transcript.export_to_file(transcript_path)
    results["artifacts"]["transcript"] = str(transcript_path)
    print(f"   ✅ Saved: {transcript_path}")
    
    # Step 8: Create activation record
    print("\n🔑 Step 8: Creating activation record...")
    activation_record = create_activation_record(
        transcript_hash=transcript_hash,
        activated_by=operator,
        active_set_version=active_set.version
    )
    
    activation_path = output_dir / "activation_record.json"
    save_activation_record(activation_record, activation_path)
    results["artifacts"]["activation"] = str(activation_path)
    print(f"   ✅ Activation ID: {activation_record.activation_id}")
    print(f"   ✅ Saved: {activation_path}")
    
    # Success!
    results["success"] = True
    results["transcript_hash"] = transcript_hash
    results["activation_id"] = str(activation_record.activation_id)
    results["pack_count"] = len(active_set.packs)
    
    print(f"\n{'='*70}")
    print(f"✅ ADMISSION CEREMONY COMPLETE")
    print(f"{'='*70}\n")
    print(f"📊 Summary:")
    print(f"   • Packs validated: {len(active_set.packs)}")
    print(f"   • Transcript hash: {transcript_hash}")
    print(f"   • Activation ID: {activation_record.activation_id}")
    print(f"   • Artifacts: {output_dir}")
    print()
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Run S1 admission ceremony for a pack set"
    )
    parser.add_argument(
        "active_set",
        type=Path,
        help="Path to active_set.json file"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("storage/admission/v1"),
        help="Output directory for artifacts (default: storage/admission/v1)"
    )
    parser.add_argument(
        "--operator",
        type=str,
        default="admission_ceremony_script",
        help="Operator identifier for activation record"
    )
    
    args = parser.parse_args()
    
    # Validate input
    if not args.active_set.exists():
        print(f"❌ Error: Active set not found: {args.active_set}")
        sys.exit(1)
    
    # Run ceremony
    results = run_ceremony(
        active_set_path=args.active_set,
        output_dir=args.output,
        operator=args.operator
    )
    
    # Export results summary
    results_path = args.output / "ceremony_results.json"
    with open(results_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2)
    
    # Exit with appropriate code
    sys.exit(0 if results["success"] else 1)


if __name__ == "__main__":
    main()
