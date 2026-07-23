"""
LDU-T Storage Adapter

Handles all database operations for Local Definition Universe - Temporary layer.
This is the mechanical, non-semantic layer that preserves raw data and ambiguity.

No semantic interpretation occurs here. Only:
- Schema validation
- Provenance tracking
- Metadata management
- Query operations

IMPORTANT: This adapter requires ENKI_DB_DSN environment variable.
Set it before using: export ENKI_DB_DSN="postgresql://user:pass@host:port/database"
"""

import json
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
from uuid import uuid4

from storage.postgres.db import get_connection
from storage.ldu_t.validators import (
    validate_ldu_t_entry_schema,
    validate_ldu_t_constraints,
    validate_ldu_t_provenance,
    ValidationResult
)


class LDUTAdapter:
    """Storage adapter for LDU-T entries (raw linguistic data)."""

    def __init__(self, tenant_id: str = "default"):
        """
        Initialize adapter for a specific tenant.
        
        Args:
            tenant_id: Multi-tenancy identifier (default: "default")
        """
        self.tenant_id = tenant_id

    def insert_entry(
        self,
        entry: Dict[str, Any],
        validate: bool = True
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Insert a single LDU-T entry into storage.
        
        Args:
            entry: LDU-T entry dict (must conform to ldu_t_entry.schema.json)
            validate: Whether to validate before insertion (default: True)
        
        Returns:
            Tuple of (success: bool, message: str, entry_id: Optional[str])
            - success=True: entry inserted, entry_id is the ID
            - success=False: validation error or DB error, entry_id is None
        
        Raises:
            Exception: Only if DB connection fails (not validation failures)
        """
        # Validation (if enabled)
        if validate:
            schema_result = validate_ldu_t_entry_schema(entry)
            if not schema_result.is_valid:
                return False, f"Schema validation failed: {schema_result.schema_errors[0]}", None
            
            constraint_result = validate_ldu_t_constraints(entry)
            if not constraint_result.is_valid:
                return False, f"Constraint violation: {constraint_result.constraint_errors[0]}", None
            
            provenance_result = validate_ldu_t_provenance(entry.get('provenance', {}))
            if not provenance_result.is_valid:
                return False, f"Provenance error: {provenance_result.provenance_errors[0]}", None
        
        # Insert into database
        entry_id = entry.get('id')
        now = datetime.now(timezone.utc).isoformat()
        
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Ensure resolution is null
                    entry_copy = entry.copy()
                    entry_copy['resolution'] = None
                    
                    # Convert ambiguity_flags to PostgreSQL JSONB array format
                    ambiguity_flags = entry.get('ambiguity_flags', [])
                    # Convert list of dicts to PostgreSQL JSONB[] format
                    ambiguity_flags_array = [json.dumps(flag) for flag in ambiguity_flags]
                    
                    cur.execute("""
                        INSERT INTO ldu_t_entries (
                            id, universe, source_type, source_metadata, 
                            content, ambiguity_flags, resolution, provenance,
                            created_at, updated_at, version
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s::jsonb[], %s, %s,
                            %s, %s, %s
                        )
                    """, (
                        entry_id,
                        'LDU-T',
                        entry.get('source', {}).get('type'),
                        json.dumps(entry.get('source', {})),
                        json.dumps(entry.get('content', {})),
                        ambiguity_flags_array,  # Pass as Python list for ARRAY conversion
                        None,  # resolution is always null
                        json.dumps(entry.get('provenance', {})),
                        now,
                        now,
                        1
                    ))
                
                conn.commit()
        except Exception as e:
            return False, f"Database error: {str(e)}", None
        
        return True, "Entry inserted successfully", entry_id

    def batch_insert(
        self,
        entries: List[Dict[str, Any]],
        ingestion_method: str,
        ingestion_actor: str,
        validate: bool = True
    ) -> Dict[str, Any]:
        """
        Insert multiple LDU-T entries in a batch operation.
        
        Args:
            entries: List of LDU-T entry dicts
            ingestion_method: Method/tool used (e.g., 'book_parser_v1.2.0')
            ingestion_actor: Who/what triggered ingestion
            validate: Whether to validate each entry (default: True)
        
        Returns:
            Receipt dict with: ingestion_id, entries_submitted, entries_accepted,
                               entries_rejected, rejected_reasons, etc.
        """
        ingestion_id = f"ingest_ldu_t_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{str(uuid4())[:8]}"
        entries_accepted = 0
        entries_rejected = 0
        rejected_reasons = []
        
        for idx, entry in enumerate(entries):
            success, message, entry_id = self.insert_entry(entry, validate=validate)
            if success:
                entries_accepted += 1
            else:
                entries_rejected += 1
                rejected_reasons.append({
                    "entry_index": idx,
                    "id": entry.get('id', 'unknown'),
                    "reason": message
                })
        
        # Record ingestion receipt
        receipt = {
            "ingestion_id": ingestion_id,
            "ingestion_timestamp": datetime.now(timezone.utc).isoformat(),
            "ingestion_method": ingestion_method,
            "ingestion_actor": ingestion_actor,
            "entries_submitted": len(entries),
            "entries_accepted": entries_accepted,
            "entries_rejected": entries_rejected,
            "rejected_reasons": rejected_reasons if rejected_reasons else None
        }
        
        self._save_ingestion_receipt(receipt)
        return receipt

    def get_entry_by_id(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """
        Retrieve a single LDU-T entry by ID.
        
        Args:
            entry_id: The entry ID to retrieve
        
        Returns:
            Entry dict if found, None otherwise
        """
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, universe, source_type, source_metadata,
                               content, ambiguity_flags, resolution, provenance,
                               created_at, updated_at, version
                        FROM ldu_t_entries
                        WHERE id = %s
                    """, (entry_id,))
                    
                    row = cur.fetchone()
                    if not row:
                        return None
                    
                    return self._row_to_dict(row)
        except Exception as e:
            print(f"Error retrieving entry {entry_id}: {str(e)}")
            return None

    def get_entries_by_source_type(
        self,
        source_type: str,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Retrieve entries by source type.
        
        Args:
            source_type: Type to filter by (book, speech, api_event, corpus, other)
            limit: Max results
            offset: Pagination offset
        
        Returns:
            List of entry dicts
        """
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, universe, source_type, source_metadata,
                               content, ambiguity_flags, resolution, provenance,
                               created_at, updated_at, version
                        FROM ldu_t_entries
                        WHERE source_type = %s
                        ORDER BY created_at DESC
                        LIMIT %s OFFSET %s
                    """, (source_type, limit, offset))
                    
                    rows = cur.fetchall()
                    return [self._row_to_dict(row) for row in rows]
        except Exception as e:
            print(f"Error retrieving entries by source type: {str(e)}")
            return []

    def get_entries_by_date_range(
        self,
        start_date: str,
        end_date: str,
        limit: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Retrieve entries created within a date range.
        
        Args:
            start_date: ISO-8601 formatted date
            end_date: ISO-8601 formatted date
            limit: Max results
        
        Returns:
            List of entry dicts
        """
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, universe, source_type, source_metadata,
                               content, ambiguity_flags, resolution, provenance,
                               created_at, updated_at, version
                        FROM ldu_t_entries
                        WHERE created_at >= %s AND created_at <= %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    """, (start_date, end_date, limit))
                    
                    rows = cur.fetchall()
                    return [self._row_to_dict(row) for row in rows]
        except Exception as e:
            print(f"Error retrieving entries by date range: {str(e)}")
            return []

    def update_metadata(
        self,
        entry_id: str,
        metadata: Dict[str, Any],
        updated_by: str
    ) -> Tuple[bool, str]:
        """
        Update metadata of an LDU-T entry WITHOUT MODIFYING CONTENT.
        
        Content is immutable. Only metadata (tags, notes, review status) can be updated.
        
        Args:
            entry_id: Entry to update
            metadata: New metadata dict (tags, notes, reviewed, etc.)
            updated_by: User/actor making the update
        
        Returns:
            Tuple of (success: bool, message: str)
        """
        # Get current entry
        entry = self.get_entry_by_id(entry_id)
        if not entry:
            return False, f"Entry {entry_id} not found"
        
        old_metadata = entry.get('metadata', {})
        
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Update main entry
                    new_version = entry.get('version', 1) + 1
                    now = datetime.now(timezone.utc).isoformat()
                    
                    cur.execute("""
                        UPDATE ldu_t_entries
                        SET metadata = %s,
                            version = %s,
                            updated_at = %s
                        WHERE id = %s
                    """, (json.dumps(metadata), new_version, now, entry_id))
                    
                    # Record in audit log
                    cur.execute("""
                        INSERT INTO ldu_t_metadata_audit (
                            ldu_t_id, old_metadata, new_metadata, changed_by
                        ) VALUES (%s, %s, %s, %s)
                    """, (entry_id, json.dumps(old_metadata), json.dumps(metadata), updated_by))
                
                conn.commit()
        except Exception as e:
            return False, f"Database error: {str(e)}"
        
        return True, f"Metadata updated (version {new_version})"

    def count_entries(self, source_type: Optional[str] = None) -> int:
        """
        Count total entries, optionally filtered by source type.
        
        Args:
            source_type: Optional filter by source type
        
        Returns:
            Count of entries
        """
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    if source_type:
                        cur.execute(
                            "SELECT COUNT(*) FROM ldu_t_entries WHERE source_type = %s",
                            (source_type,)
                        )
                    else:
                        cur.execute("SELECT COUNT(*) FROM ldu_t_entries")
                    
                    return cur.fetchone()[0]
        except Exception as e:
            print(f"Error counting entries: {str(e)}")
            return 0

    # Private helpers

    def _row_to_dict(self, row: Tuple) -> Dict[str, Any]:
        """Convert database row to entry dict."""
        return {
            'id': row[0],
            'universe': row[1],
            'source': json.loads(row[3]),  # source_metadata
            'content': json.loads(row[4]),
            'ambiguity_flags': json.loads(row[5]),
            'resolution': row[6],  # Always None
            'provenance': json.loads(row[7]),
            'created_at': row[8].isoformat() if row[8] else None,
            'updated_at': row[9].isoformat() if row[9] else None,
            'version': row[10]
        }

    def _save_ingestion_receipt(self, receipt: Dict[str, Any]) -> bool:
        """Save ingestion receipt to database."""
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO ldu_t_ingestion_receipts (
                            ingestion_id, ingestion_timestamp, ingestion_method,
                            ingestion_actor, entries_submitted, entries_accepted,
                            entries_rejected, rejected_details
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        receipt['ingestion_id'],
                        receipt['ingestion_timestamp'],
                        receipt['ingestion_method'],
                        receipt['ingestion_actor'],
                        receipt['entries_submitted'],
                        receipt['entries_accepted'],
                        receipt['entries_rejected'],
                        json.dumps(receipt.get('rejected_reasons'))
                    ))
                
                conn.commit()
            return True
        except Exception as e:
            print(f"Error saving ingestion receipt: {str(e)}")
            return False
