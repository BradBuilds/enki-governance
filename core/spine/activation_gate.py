"""
ENKI Spine S1.5 — Activation Gate

Enforces explicit activation requirement: Nothing is active unless declared.
Declarative checkpoint requiring human review and acceptance of validation transcript.

NO HARDWARE VALIDATION YET — This is declaration-only (S1.5 scope).

Authority: SPINE-00 v1.0.1, SPINE-EXECUTION-PLAN S1.5
"""

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from pathlib import Path


@dataclass(frozen=True)
class ActivationRecord:
    """
    Immutable activation record.
    
    Declares explicit acceptance of a validation transcript.
    Once created, cannot be modified (frozen dataclass).
    """
    activation_id: str  # UUID v4 (unique identifier)
    active_set_version: str  # References active set version
    transcript_hash: str  # SHA-256 hash of validation transcript
    activated_at: str  # ISO 8601 UTC timestamp
    activated_by: str  # Identifier (user, device, or system)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "activation_id": self.activation_id,
            "active_set_version": self.active_set_version,
            "transcript_hash": self.transcript_hash,
            "activated_at": self.activated_at,
            "activated_by": self.activated_by
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "ActivationRecord":
        """Create from dictionary."""
        return cls(
            activation_id=data["activation_id"],
            active_set_version=data["active_set_version"],
            transcript_hash=data["transcript_hash"],
            activated_at=data["activated_at"],
            activated_by=data["activated_by"]
        )


def create_activation_record(
    active_set_version: str,
    transcript_hash: str,
    activated_by: str
) -> ActivationRecord:
    """
    Create new activation record.
    
    Args:
        active_set_version: Version of active set being activated
        transcript_hash: SHA-256 hash of validation transcript (from S1.4)
        activated_by: Identifier of activator (e.g., "developer", "admin", device ID)
    
    Returns:
        Immutable ActivationRecord
    """
    activation_id = str(uuid.uuid4())
    activated_at = datetime.now(timezone.utc).isoformat()
    
    return ActivationRecord(
        activation_id=activation_id,
        active_set_version=active_set_version,
        transcript_hash=transcript_hash,
        activated_at=activated_at,
        activated_by=activated_by
    )


def verify_activation(
    activation_record: ActivationRecord,
    computed_transcript_hash: str
) -> bool:
    """
    Verify activation record against computed transcript hash.
    
    Args:
        activation_record: Declared activation
        computed_transcript_hash: Hash computed from current validation transcript
    
    Returns:
        True if hashes match, False otherwise
    """
    return activation_record.transcript_hash == computed_transcript_hash


def save_activation_record(
    activation_record: ActivationRecord,
    file_path: str
) -> None:
    """
    Save activation record to JSON file.
    
    Args:
        activation_record: Record to save
        file_path: Absolute path for output file
    """
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    data = {
        "version": "1.0.0",
        "activation": activation_record.to_dict()
    }
    
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_activation_record(file_path: str) -> ActivationRecord:
    """
    Load activation record from JSON file.
    
    Args:
        file_path: Absolute path to activation record file
    
    Returns:
        Loaded ActivationRecord
    
    Raises:
        FileNotFoundError: If file doesn't exist
        ValueError: If file format invalid
    """
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    if "activation" not in data:
        raise ValueError("Invalid activation record: missing 'activation' field")
    
    return ActivationRecord.from_dict(data["activation"])


def find_latest_activation(directory_path: str, active_set_version: str) -> Optional[ActivationRecord]:
    """
    Find latest activation record for given active set version.
    
    Args:
        directory_path: Directory containing activation records
        active_set_version: Active set version to search for
    
    Returns:
        Latest ActivationRecord, or None if not found
    """
    directory = Path(directory_path)
    
    if not directory.exists():
        return None
    
    # Find all .json files
    activation_files = list(directory.glob("activation_*.json"))
    
    if not activation_files:
        return None
    
    # Load and filter by active_set_version
    matching_records = []
    for file_path in activation_files:
        try:
            record = load_activation_record(str(file_path))
            if record.active_set_version == active_set_version:
                matching_records.append(record)
        except (ValueError, KeyError, json.JSONDecodeError):
            # Skip invalid files
            continue
    
    if not matching_records:
        return None
    
    # Return latest by activated_at timestamp
    return max(matching_records, key=lambda r: r.activated_at)


class ActivationError(Exception):
    """Base class for activation errors."""
    pass


class ActivationRequiredError(ActivationError):
    """Raised when activation record is required but missing."""
    
    def __init__(self, active_set_version: str, transcript_hash: str):
        self.active_set_version = active_set_version
        self.transcript_hash = transcript_hash
        super().__init__(
            f"Activation required for active set {active_set_version} "
            f"(transcript hash: {transcript_hash[:16]}...)"
        )


class ActivationTranscriptMismatchError(ActivationError):
    """Raised when activation transcript hash doesn't match computed hash."""
    
    def __init__(self, activation_id: str, declared_hash: str, computed_hash: str):
        self.activation_id = activation_id
        self.declared_hash = declared_hash
        self.computed_hash = computed_hash
        super().__init__(
            f"Activation {activation_id} transcript hash mismatch: "
            f"declared {declared_hash[:16]}... != computed {computed_hash[:16]}..."
        )


def require_activation(
    active_set_version: str,
    transcript_hash: str,
    activation_directory: str
) -> ActivationRecord:
    """
    Require valid activation record for given active set and transcript.
    
    Args:
        active_set_version: Active set version requiring activation
        transcript_hash: Computed transcript hash
        activation_directory: Directory containing activation records
    
    Returns:
        Valid ActivationRecord
    
    Raises:
        ActivationRequiredError: If no activation found
        ActivationTranscriptMismatchError: If activation hash doesn't match
    """
    # Find latest activation for this active set version
    activation = find_latest_activation(activation_directory, active_set_version)
    
    if activation is None:
        raise ActivationRequiredError(active_set_version, transcript_hash)
    
    # Verify transcript hash matches
    if not verify_activation(activation, transcript_hash):
        raise ActivationTranscriptMismatchError(
            activation.activation_id,
            activation.transcript_hash,
            transcript_hash
        )
    
    return activation
