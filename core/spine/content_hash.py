"""
ENKI Spine S1.3 — Content Hash Enforcement

Implements cryptographic integrity verification per SPINE-00 §1.1-1.3.
Provides deterministic, cross-platform pack hash computation using:
- SHA-256
- RFC 8785 JCS (JSON Canonicalization Scheme)
- Unicode NFC normalization

Authority: SPINE-00 v1.0.1, SPINE-EXECUTION-PLAN S1.3
"""

import hashlib
import json
import unicodedata
from pathlib import Path
from typing import Dict, Any, Optional, List
import os


# Hash format prefix (LOCKED)
HASH_PREFIX = "sha256:"


def compute_pack_hash(pack_path: str) -> str:
    """
    Compute canonical content hash for a pack.
    
    Algorithm (SPINE-00 §1.1):
    1. Load and normalize pack.json (JCS + Unicode NFC)
    2. Load all definition files listed in manifest (sorted lexicographically)
    3. Hash each file deterministically (JSON files use JCS)
    4. Combine all into single SHA-256 digest
    
    Args:
        pack_path: Absolute path to pack directory
    
    Returns:
        Hash string in format: "sha256:<hex_digest>"
    
    Raises:
        FileNotFoundError: If pack.json or referenced files missing
        ValueError: If JSON parsing fails or structure invalid
    """
    pack_dir = Path(pack_path)
    
    # Load pack.json
    manifest_path = pack_dir / "pack.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"pack.json not found in {pack_path}")
    
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    
    # Start hash with normalized pack.json
    hasher = hashlib.sha256()
    manifest_bytes = normalize_json_bytes(manifest)
    hasher.update(manifest_bytes)
    
    # Get definition files from manifest (if present)
    definitions = manifest.get("definitions", [])
    
    # Extract file paths and sort lexicographically (deterministic ordering)
    file_paths = []
    for defn in definitions:
        if isinstance(defn, dict) and "path" in defn:
            file_paths.append(defn["path"])

    # Policy and lexical packs list their files under "manifest" (plain relative
    # paths) instead of "definitions". Those files are content too: without this a
    # policy pack's weights could change and its hash would not. Packs that only
    # use "definitions" hash exactly as before.
    for entry in manifest.get("manifest", []):
        if isinstance(entry, str):
            file_paths.append(entry)
        elif isinstance(entry, dict) and "path" in entry:
            file_paths.append(entry["path"])

    file_paths = sorted(set(file_paths))  # Lexicographic ordering, each file once
    
    # Hash each definition file
    for rel_path in file_paths:
        file_path = pack_dir / rel_path
        
        if not file_path.exists():
            raise FileNotFoundError(f"Definition file not found: {rel_path}")
        
        file_bytes = _hash_file(str(file_path))
        hasher.update(file_bytes)
    
    # Return formatted hash
    digest = hasher.hexdigest()
    return f"{HASH_PREFIX}{digest}"


def verify_pack_hash(pack_path: str, declared_hash: str) -> bool:
    """
    Verify pack content matches declared hash.
    
    Args:
        pack_path: Absolute path to pack directory
        declared_hash: Expected hash (format: "sha256:<hex>")
    
    Returns:
        True if hashes match, False otherwise
    
    Raises:
        FileNotFoundError: If pack files missing
        ValueError: If hash computation fails
    """
    computed_hash = compute_pack_hash(pack_path)
    return computed_hash == declared_hash


def normalize_json_bytes(json_data: Dict[str, Any]) -> bytes:
    """
    Normalize JSON to canonical bytes per RFC 8785 JCS.
    
    Steps:
    1. Serialize using JCS (sort_keys, no whitespace, strict float handling)
    2. Apply Unicode NFC normalization to string values
    3. Return UTF-8 encoded bytes
    
    Args:
        json_data: Dictionary to normalize
    
    Returns:
        Canonical UTF-8 bytes
    
    Note:
        This is a simplified JCS implementation. Full RFC 8785 compliance
        would require handling of number normalization, but for ENKI's
        use case (semantic version strings, not floating point), this suffices.
    """
    # Serialize with sorted keys (JCS requirement)
    json_str = json.dumps(
        json_data,
        ensure_ascii=False,  # Preserve Unicode
        sort_keys=True,      # Deterministic key ordering
        separators=(',', ':'),  # No whitespace
        allow_nan=False      # Reject NaN/Infinity (SPINE-00 requirement)
    )
    
    # Apply Unicode NFC normalization
    normalized_str = unicodedata.normalize('NFC', json_str)
    
    # Return UTF-8 bytes
    return normalized_str.encode('utf-8')


def _hash_file(file_path: str) -> bytes:
    """
    Hash a single file deterministically.
    
    Rules:
    - If file extension is .json: Parse and re-serialize using JCS
    - Otherwise: Hash raw bytes exactly (no newline conversion)
    
    Args:
        file_path: Absolute path to file
    
    Returns:
        File content bytes (normalized if JSON)
    """
    path = Path(file_path)
    
    if path.suffix.lower() == '.json':
        # JSON file: parse and normalize
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return normalize_json_bytes(data)
    else:
        # Non-JSON: hash raw bytes
        with open(path, 'rb') as f:
            return f.read()


def compute_transcript_hash(transcript_entries: List[Dict[str, Any]]) -> str:
    """
    Compute hash of validation transcript (for S1.4/S1.5 integration).
    
    Uses same canonicalization as pack hashing (JCS + NFC).
    
    Args:
        transcript_entries: List of transcript entry dicts
    
    Returns:
        Hash string: "sha256:<hex_digest>"
    """
    # Wrap in deterministic structure
    transcript_data = {"entries": transcript_entries}
    
    # Normalize and hash
    transcript_bytes = normalize_json_bytes(transcript_data)
    digest = hashlib.sha256(transcript_bytes).hexdigest()
    
    return f"{HASH_PREFIX}{digest}"


def validate_hash_format(hash_str: str) -> bool:
    """
    Validate hash string format.
    
    Expected format: "sha256:" followed by 64 hex characters
    
    Args:
        hash_str: Hash string to validate
    
    Returns:
        True if valid format, False otherwise
    """
    if not hash_str.startswith(HASH_PREFIX):
        return False
    
    hex_part = hash_str[len(HASH_PREFIX):]
    
    if len(hex_part) != 64:
        return False
    
    try:
        int(hex_part, 16)  # Verify all characters are hex
        return True
    except ValueError:
        return False
