"""
ENKI Definition Pack Validator - CLI Tool

Command-line interface for validating definition packs.

Usage:
    python -m core.tools.validate_pack <pack_path>

Example:
    python -m core.tools.validate_pack libraries/base60

Exit codes:
    0 - Pack is valid
    1 - Pack has errors

Phase 2: Emits audit events for pack loading/validation.
"""

import sys
from pathlib import Path
from core.packs import load_pack
from core.events import emit_event, EventTypes, Severity, Scope


def main():
    """Main entry point for CLI validation."""
    if len(sys.argv) < 2:
        print("Usage: python -m core.tools.validate_pack <pack_path>")
        print("Example: python -m core.tools.validate_pack libraries/base60")
        sys.exit(1)

    pack_path = sys.argv[1]
    
    print(f"Validating pack: {pack_path}")
    print("-" * 60)
    
    # Emit validation start event
    emit_event(
        event_type=EventTypes.VALIDATION_STARTED,
        severity=Severity.INFO,
        scope=Scope.LOCAL,
        actor_id="cli_validator",
        metadata={
            "validation_type": "pack",
            "pack_path": pack_path
        }
    )
    
    # Load the pack
    result = load_pack(pack_path)
    
    # Print summary
    if result.ok:
        print("[OK] PACK VALID")
        print()
        print(f"Pack ID:      {result.pack_id}")
        print(f"Version:      {result.version.get('version', 'N/A')}")
        print(f"Name:         {result.version.get('name', 'N/A')}")
        print(f"Definitions:  {result.definition_count()}")
        
        # Emit pack loaded event
        emit_event(
            event_type=EventTypes.PACK_LOADED,
            severity=Severity.INFO,
            scope=Scope.LOCAL,
            actor_id="cli_validator",
            pack_id=result.pack_id,
            metadata={
                "pack_path": pack_path,
                "version": result.version.get('version'),
                "definition_count": result.definition_count(),
                "warning_count": len(result.warnings)
            }
        )
        
        # Print definition breakdown by type
        if not result.index.is_empty():
            print()
            print("Definitions by type:")
            for def_type, defs in sorted(result.index.by_type.items()):
                print(f"  {def_type:12s} {len(defs):3d}")
        
        # Print warnings if any
        if result.has_warnings():
            print()
            print("[WARN] WARNINGS:")
            for warning in result.warnings:
                print(f"  - {warning}")
        
        # Emit usage meter tick
        emit_event(
            event_type=EventTypes.USAGE_METER_TICK,
            severity=Severity.INFO,
            scope=Scope.LOCAL,
            actor_id="cli_validator",
            metadata={
                "operation": "pack_validation",
                "result": "success",
                "definition_count": result.definition_count()
            }
        )
        
        print()
        print("-" * 60)
        sys.exit(0)
    
    else:
        print("[ERROR] PACK INVALID")
        print()
        
        if result.pack_id:
            print(f"Pack ID: {result.pack_id}")
        
        # Emit pack validation failed event
        emit_event(
            event_type=EventTypes.PACK_VALIDATION_FAILED,
            severity=Severity.ERROR,
            scope=Scope.LOCAL,
            actor_id="cli_validator",
            pack_id=result.pack_id if result.pack_id else "unknown",
            metadata={
                "pack_path": pack_path,
                "error_count": len(result.errors),
                "warning_count": len(result.warnings),
                "first_error": result.errors[0] if result.errors else None
            }
        )
        
        # Print errors
        if result.has_errors():
            print(f"\n[ERROR] ERRORS ({len(result.errors)}):")
            for error in result.errors:
                print(f"  - {error}")
        
        # Print warnings if any
        if result.has_warnings():
            print(f"\n[WARN] WARNINGS ({len(result.warnings)}):")
            for warning in result.warnings:
                print(f"  - {warning}")
        
        # Emit usage meter tick
        emit_event(
            event_type=EventTypes.USAGE_METER_TICK,
            severity=Severity.INFO,
            scope=Scope.LOCAL,
            actor_id="cli_validator",
            metadata={
                "operation": "pack_validation",
                "result": "failure",
                "error_count": len(result.errors)
            }
        )
        
        print()
        print("-" * 60)
        sys.exit(1)


if __name__ == "__main__":
    main()
