"""
ENKI Spine S1.4 — Validation Transcript

Records deterministic, replayable validation history.
Creates tamper-evident audit trail of all spine validation steps.

Authority: SPINE-00 v1.0.1, SPINE-EXECUTION-PLAN S1.4
"""

import json
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional
from enum import Enum
from datetime import datetime, timezone
from pathlib import Path
from core.spine.content_hash import compute_transcript_hash


class ValidationOutcome(Enum):
    """Validation step outcome."""
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"


@dataclass
class TranscriptEntry:
    """
    Single validation step record.
    
    Immutable record of one validation action with deterministic fields.
    """
    step_type: str  # e.g., "capability_validation", "namespace_check", "hash_verification"
    pack_id: str  # Which pack was validated
    outcome: str  # "pass", "fail", or "skipped"
    sequence_number: int  # Monotonic counter (starts at 1)
    
    error_code: Optional[str] = None  # Present if outcome="fail"
    context: Dict[str, Any] = field(default_factory=dict)  # Structured validation context
    timestamp: Optional[str] = None  # ISO 8601 UTC (for audit, not included in hash)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "step_type": self.step_type,
            "pack_id": self.pack_id,
            "outcome": self.outcome,
            "sequence_number": self.sequence_number
        }
        
        if self.error_code:
            result["error_code"] = self.error_code
        
        if self.context:
            result["context"] = self.context
        
        if self.timestamp:
            result["timestamp"] = self.timestamp
        
        return result
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TranscriptEntry":
        """Create from dictionary."""
        return cls(
            step_type=data["step_type"],
            pack_id=data["pack_id"],
            outcome=data["outcome"],
            sequence_number=data["sequence_number"],
            error_code=data.get("error_code"),
            context=data.get("context", {}),
            timestamp=data.get("timestamp")
        )


@dataclass
class ValidationTranscript:
    """
    Validation transcript - deterministic record of all validation steps.
    
    Provides:
    - Chronological record of validation events
    - Deterministic hash for activation verification
    - JSON export for audit/replay
    - Append-only semantics (no mutation after creation)
    """
    entries: List[TranscriptEntry] = field(default_factory=list)
    _next_sequence: int = field(default=1, init=False)
    
    def record_step(
        self,
        step_type: str,
        pack_id: str,
        outcome: ValidationOutcome,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        include_timestamp: bool = True
    ) -> TranscriptEntry:
        """
        Record a validation step.
        
        Args:
            step_type: Type of validation (e.g., "capability_resolution")
            pack_id: Pack being validated
            outcome: Pass/fail/skipped
            error_code: Error code if outcome=fail
            context: Additional structured context
            include_timestamp: Whether to add timestamp (True for audit, False for deterministic hashing)
        
        Returns:
            Created TranscriptEntry
        """
        timestamp = None
        if include_timestamp:
            timestamp = datetime.now(timezone.utc).isoformat()
        
        entry = TranscriptEntry(
            step_type=step_type,
            pack_id=pack_id,
            outcome=outcome.value,
            sequence_number=self._next_sequence,
            error_code=error_code,
            context=context or {},
            timestamp=timestamp
        )
        
        self.entries.append(entry)
        self._next_sequence += 1
        
        return entry
    
    def compute_hash(self, include_timestamps: bool = False) -> str:
        """
        Compute deterministic hash of transcript.
        
        Args:
            include_timestamps: Whether to include timestamps in hash (False for determinism)
        
        Returns:
            SHA-256 hash: "sha256:<hex>"
        
        Note:
            For activation verification (S1.5), timestamps should NOT be included
            to ensure deterministic hashing across different execution times.
        """
        # Convert entries to dicts
        entries_data = []
        for entry in self.entries:
            entry_dict = entry.to_dict()
            
            # Remove timestamp if not including in hash
            if not include_timestamps and "timestamp" in entry_dict:
                del entry_dict["timestamp"]
            
            entries_data.append(entry_dict)
        
        # Compute hash using same canonicalization as packs
        return compute_transcript_hash(entries_data)
    
    def export_to_file(self, file_path: str) -> None:
        """
        Export transcript to JSON file.
        
        Args:
            file_path: Absolute path for output file
        """
        data = {
            "version": "1.0.0",
            "entries": [entry.to_dict() for entry in self.entries],
            "entry_count": len(self.entries)
        }
        
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    
    @classmethod
    def load_from_file(cls, file_path: str) -> "ValidationTranscript":
        """
        Load transcript from JSON file.
        
        Args:
            file_path: Absolute path to transcript file
        
        Returns:
            Loaded ValidationTranscript
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        transcript = cls()
        
        for entry_data in data.get("entries", []):
            entry = TranscriptEntry.from_dict(entry_data)
            transcript.entries.append(entry)
        
        # Update sequence counter
        if transcript.entries:
            transcript._next_sequence = max(e.sequence_number for e in transcript.entries) + 1
        
        return transcript
    
    def get_failures(self) -> List[TranscriptEntry]:
        """Get all failed validation steps."""
        return [e for e in self.entries if e.outcome == ValidationOutcome.FAIL.value]
    
    def get_passes(self) -> List[TranscriptEntry]:
        """Get all passed validation steps."""
        return [e for e in self.entries if e.outcome == ValidationOutcome.PASS.value]
    
    def has_failures(self) -> bool:
        """Check if transcript contains any failures."""
        return any(e.outcome == ValidationOutcome.FAIL.value for e in self.entries)
    
    def get_steps_by_type(self, step_type: str) -> List[TranscriptEntry]:
        """Get all steps of a specific type."""
        return [e for e in self.entries if e.step_type == step_type]
    
    def get_steps_for_pack(self, pack_id: str) -> List[TranscriptEntry]:
        """Get all validation steps for a specific pack."""
        return [e for e in self.entries if e.pack_id == pack_id]
    
    def is_empty(self) -> bool:
        """Check if transcript has no entries."""
        return len(self.entries) == 0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert transcript to dictionary."""
        return {
            "version": "1.0.0",
            "entries": [entry.to_dict() for entry in self.entries],
            "entry_count": len(self.entries)
        }
    
    def export_canonical(self, include_timestamps: bool = False) -> Dict[str, Any]:
        """
        Export transcript in canonical form for deterministic comparison.
        
        This method produces a deterministic JSON structure by:
        - Sorting entries by (pack_id, step_type, sequence_number)
        - Sorting context keys alphabetically
        - Optionally excluding timestamps
        - Using consistent field ordering
        
        Args:
            include_timestamps: Whether to include timestamps (False for determinism)
        
        Returns:
            Canonicalized dictionary suitable for hashing or comparison
            
        Authority: GDU Technical Build Plan v2.4.0 §Phase 2.1
        """
        canonical_entries = []
        
        # Sort entries deterministically (pack_id, step_type, sequence_number)
        sorted_entries = sorted(
            self.entries,
            key=lambda e: (e.pack_id, e.step_type, e.sequence_number)
        )
        
        for entry in sorted_entries:
            entry_dict = {
                "pack_id": entry.pack_id,
                "step_type": entry.step_type,
                "outcome": entry.outcome,
                "sequence_number": entry.sequence_number
            }
            
            if entry.error_code:
                entry_dict["error_code"] = entry.error_code
            
            # Sort context keys for determinism
            if entry.context:
                entry_dict["context"] = dict(sorted(entry.context.items()))
            
            # Include timestamp only if requested
            if include_timestamps and entry.timestamp:
                entry_dict["timestamp"] = entry.timestamp
            
            canonical_entries.append(entry_dict)
        
        return {
            "version": "1.0.0",
            "entries": canonical_entries,
            "entry_count": len(canonical_entries)
        }


def create_transcript_from_validation(
    namespace_errors: List,
    capability_errors: List,
    hash_errors: List,
    packs: List[str]
) -> ValidationTranscript:
    """
    Create transcript from validation results (helper for integration).
    
    Args:
        namespace_errors: List of namespace validation errors
        capability_errors: List of capability resolution errors
        hash_errors: List of hash verification errors
        packs: List of pack IDs that were validated
    
    Returns:
        Complete ValidationTranscript
    """
    transcript = ValidationTranscript()
    
    # Record namespace validations
    for pack_id in packs:
        pack_namespace_errors = [e for e in namespace_errors if e.context.get("pack_id") == pack_id]
        
        if pack_namespace_errors:
            for error in pack_namespace_errors:
                transcript.record_step(
                    step_type="namespace_validation",
                    pack_id=pack_id,
                    outcome=ValidationOutcome.FAIL,
                    error_code=error.code,
                    context=error.context
                )
        else:
            transcript.record_step(
                step_type="namespace_validation",
                pack_id=pack_id,
                outcome=ValidationOutcome.PASS
            )
    
    # Record capability validations
    for pack_id in packs:
        pack_cap_errors = [e for e in capability_errors if e.context.get("pack_id") == pack_id or e.context.get("consumer_pack_id") == pack_id]
        
        if pack_cap_errors:
            for error in pack_cap_errors:
                transcript.record_step(
                    step_type="capability_validation",
                    pack_id=pack_id,
                    outcome=ValidationOutcome.FAIL,
                    error_code=error.code,
                    context=error.context
                )
        else:
            transcript.record_step(
                step_type="capability_validation",
                pack_id=pack_id,
                outcome=ValidationOutcome.PASS
            )
    
    # Record hash verifications
    for pack_id in packs:
        pack_hash_errors = [e for e in hash_errors if e.context.get("pack_id") == pack_id]
        
        if pack_hash_errors:
            for error in pack_hash_errors:
                transcript.record_step(
                    step_type="hash_verification",
                    pack_id=pack_id,
                    outcome=ValidationOutcome.FAIL,
                    error_code=error.code,
                    context=error.context
                )
        else:
            transcript.record_step(
                step_type="hash_verification",
                pack_id=pack_id,
                outcome=ValidationOutcome.PASS
            )
    
    return transcript
