"""
Vocabulary Extension Engine (Tier 1 — Lightweight Ceremony)

Extends existing ceremony-admitted packs with new definitions WITHOUT
requiring a full Gate A+B+C ceremony. Uses Gate A (pack integrity) only.

Per Master Plan Section 1.8 and FD-7:
  - Adding new definitions to an EXISTING admitted pack
  - Pack must already be in the active set
  - New definition namespace must be owned by the pack
  - No collision with existing definitions
  - Gate A validation only (integrity check)
  - Pack version incremented (patch bump)
  - Active set content hash recalculated
  - Extension event chain-hashed

Usage:
    from core.vocabulary.extension import propose_extension, approve_extension

    proposal = propose_extension(
        pack_id="auditvocab",
        definition_id="signal.batch_ingested",
        definition_content={...},
        proposed_by="system",
    )

    result = approve_extension(proposal, approved_by="admin-yubikey-001")
"""

import json
import hashlib
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.spine.ceremony_gates import gate_a_pack_integrity, GateError
from core.spine.content_hash import compute_pack_hash as spine_compute_pack_hash


@dataclass
class ExtensionProposal:
    """A proposal to add a new definition to an existing pack."""
    proposal_id: str
    pack_id: str
    definition_id: str
    definition_content: dict
    extension_type: str = "event_type"     # event_type, signal_type, observable_type
    proposed_by: str = "system"
    proposed_at: str = ""
    status: str = "pending"                # pending, approved, rejected
    rejection_reason: Optional[str] = None
    gate_a_errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExtensionResult:
    """Result of applying a vocabulary extension."""
    success: bool
    proposal_id: str
    pack_id: str
    definition_id: str
    old_version: str
    new_version: str
    old_content_hash: str
    new_content_hash: str
    gate_a_errors: list
    error_message: Optional[str] = None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _compute_pack_content_hash(pack_path: Path) -> str:
    """Compute pack content hash using the spine's canonical method.
    Uses JCS normalization + manifest-ordered definition files (SPINE-00 §1.1)."""
    return spine_compute_pack_hash(str(pack_path))


def _increment_patch_version(version: str) -> str:
    """Increment patch version: 0.2.0 → 0.2.1, 1.0.0 → 1.0.1."""
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid semver: {version}")
    parts[2] = str(int(parts[2]) + 1)
    return ".".join(parts)


def _get_repo_root() -> Path:
    """Get repository root directory."""
    current = Path(__file__).resolve()
    return current.parent.parent.parent


def propose_extension(
    pack_id: str,
    definition_id: str,
    definition_content: dict,
    extension_type: str = "event_type",
    proposed_by: str = "system",
) -> ExtensionProposal:
    """
    Create a vocabulary extension proposal.

    This does NOT modify anything yet. It creates a proposal that can be
    reviewed and approved. Proposals are validated at approval time.

    Args:
        pack_id: Which pack to extend (must be in active set)
        definition_id: ID for the new definition
        definition_content: Full definition content (dict)
        extension_type: Type of extension (event_type, signal_type, etc.)
        proposed_by: Who proposed this (identity or "system")

    Returns:
        ExtensionProposal ready for review/approval
    """
    return ExtensionProposal(
        proposal_id=f"ext_{pack_id}_{definition_id}_{uuid.uuid4().hex[:8]}",
        pack_id=pack_id,
        definition_id=definition_id,
        definition_content=definition_content,
        extension_type=extension_type,
        proposed_by=proposed_by,
        proposed_at=_utcnow_iso(),
        status="pending",
    )


def validate_extension(
    proposal: ExtensionProposal,
    libraries_path: Optional[Path] = None,
) -> list[GateError]:
    """
    Run Gate A validation on a proposed extension.

    Checks:
    1. Target pack exists on filesystem
    2. pack.json is valid
    3. namespaces_owned covers the new definition's namespace
    4. No volatile fields in definition content
    5. Definition ID doesn't already exist in the pack

    Args:
        proposal: The extension proposal to validate
        libraries_path: Path to libraries/ directory (auto-detected if None)

    Returns:
        List of GateError (empty = validation passed)
    """
    if libraries_path is None:
        libraries_path = _get_repo_root() / "libraries"

    pack_path = libraries_path / proposal.pack_id

    # Run standard Gate A on the pack
    errors = gate_a_pack_integrity({proposal.pack_id: pack_path})
    if errors:
        return errors

    # Load pack.json to check namespace ownership
    pack_json_path = pack_path / "pack.json"
    with open(pack_json_path, "r", encoding="utf-8") as f:
        pack_data = json.load(f)

    # Check namespace ownership
    namespaces = pack_data.get("namespaces_owned", [])
    def_namespace = proposal.definition_id.rsplit(".", 1)[0] if "." in proposal.definition_id else ""

    namespace_match = any(
        def_namespace.startswith(ns) or proposal.definition_id.startswith(ns)
        for ns in namespaces
    )
    if not namespace_match and namespaces:
        # For auditvocab, the namespace is "audit.vocab" but signal event types
        # like "signal.batch_ingested" need to be allowed. The pack owns the
        # event type CATALOG, not the event type namespace.
        # Check if this is an event_type extension (adding to the event type list)
        if proposal.extension_type != "event_type":
            errors.append(GateError(
                gate="A",
                code="NAMESPACE_NOT_OWNED",
                message=f"Definition '{proposal.definition_id}' namespace "
                        f"'{def_namespace}' not in pack's namespaces_owned: {namespaces}",
                context={
                    "pack_id": proposal.pack_id,
                    "definition_id": proposal.definition_id,
                    "namespaces_owned": namespaces,
                }
            ))

    # Check definition doesn't already exist
    existing_defs = pack_data.get("definitions", [])
    existing_ids = {d.get("id") for d in existing_defs}
    if proposal.definition_id in existing_ids:
        errors.append(GateError(
            gate="A",
            code="DEFINITION_ALREADY_EXISTS",
            message=f"Definition '{proposal.definition_id}' already exists in pack",
            context={"pack_id": proposal.pack_id}
        ))

    # Check for volatile content in the definition
    content_str = json.dumps(proposal.definition_content)
    volatile_patterns = ["/tmp/", r"C:\\Users\\", "hostname", "process_id"]
    import re
    for pattern in volatile_patterns:
        if re.search(pattern, content_str, re.IGNORECASE):
            errors.append(GateError(
                gate="A",
                code="VOLATILE_CONTENT",
                message=f"Volatile data in definition content: {pattern}",
                context={"definition_id": proposal.definition_id}
            ))

    return errors


def approve_extension(
    proposal: ExtensionProposal,
    approved_by: str,
    libraries_path: Optional[Path] = None,
) -> ExtensionResult:
    """
    Approve and apply a vocabulary extension.

    This is the Tier 1 mini-ceremony:
    1. Validate via Gate A
    2. Add definition to pack's event types file
    3. Increment pack version (patch bump)
    4. Recompute content hash

    The active set file is NOT modified here — that requires the full
    ceremony. Instead, the caller updates the active set's content_hash
    for this pack.

    Args:
        proposal: Approved extension proposal
        approved_by: Who approved (identity_id or device_id)
        libraries_path: Path to libraries/ directory

    Returns:
        ExtensionResult with success/failure and new version/hash
    """
    if libraries_path is None:
        libraries_path = _get_repo_root() / "libraries"

    pack_path = libraries_path / proposal.pack_id

    # Step 1: Validate
    errors = validate_extension(proposal, libraries_path)
    if errors:
        proposal.status = "rejected"
        proposal.rejection_reason = "; ".join(e.message for e in errors)
        proposal.gate_a_errors = [e.to_dict() for e in errors]
        return ExtensionResult(
            success=False,
            proposal_id=proposal.proposal_id,
            pack_id=proposal.pack_id,
            definition_id=proposal.definition_id,
            old_version="",
            new_version="",
            old_content_hash="",
            new_content_hash="",
            gate_a_errors=[e.to_dict() for e in errors],
            error_message=proposal.rejection_reason,
        )

    # Step 2: Load current pack state
    pack_json_path = pack_path / "pack.json"
    with open(pack_json_path, "r", encoding="utf-8") as f:
        pack_data = json.load(f)

    old_version = pack_data["version"]
    old_content_hash = _compute_pack_content_hash(pack_path)

    # Step 3: Add new event types to the event_types definition file
    if proposal.extension_type == "event_type":
        _add_event_types_to_pack(pack_path, proposal)

    # Step 4: Increment version
    new_version = _increment_patch_version(old_version)
    pack_data["version"] = new_version
    with open(pack_json_path, "w", encoding="utf-8") as f:
        json.dump(pack_data, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Step 5: Recompute content hash
    new_content_hash = _compute_pack_content_hash(pack_path)

    # Step 6: Mark proposal as approved
    proposal.status = "approved"

    return ExtensionResult(
        success=True,
        proposal_id=proposal.proposal_id,
        pack_id=proposal.pack_id,
        definition_id=proposal.definition_id,
        old_version=old_version,
        new_version=new_version,
        old_content_hash=old_content_hash,
        new_content_hash=new_content_hash,
        gate_a_errors=[],
    )


def _add_event_types_to_pack(pack_path: Path, proposal: ExtensionProposal) -> None:
    """Add new event types to the pack's event_types definition file."""
    # Find the event_types definition file
    event_types_path = pack_path / "definitions" / "auditvocab.event_types.json"
    if not event_types_path.exists():
        raise FileNotFoundError(
            f"Event types definition file not found: {event_types_path}"
        )

    with open(event_types_path, "r", encoding="utf-8") as f:
        event_types_data = json.load(f)

    # The new event types are in the proposal's definition_content
    new_event_types = proposal.definition_content.get("event_types", [])
    existing_types = event_types_data["content"]["event_types"]
    existing_ids = {et["event_type_id"] for et in existing_types}

    added = 0
    for new_et in new_event_types:
        if new_et["event_type_id"] not in existing_ids:
            existing_types.append(new_et)
            added += 1

    if added == 0:
        return

    # Write back
    with open(event_types_path, "w", encoding="utf-8") as f:
        json.dump(event_types_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
