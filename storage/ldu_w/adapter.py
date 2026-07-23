"""
LDU-W Adapter

CRUD operations for workable definitions:
- Insert with full validation (schema + scope + lineage + conformance)
- Update metadata only (immutable fields blocked by DB trigger)
- Update status only (immutable fields blocked by DB trigger)
- Query by ID, by scope, by LDU-T source (lineage reverse query)
- Batch insert with ingestion receipts

All mutations validated before DB insertion.
Database trigger enforces immutability (defensive).
"""

import json
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timezone
import psycopg2
from psycopg2.extras import Json, RealDictCursor

from storage.ldu_w.validators import (
    validate_ldu_w_full,
    ValidationResult
)
from storage.postgres.db import get_connection


class LDUWAdapter:
    """Adapter for LDU-W workable definition operations."""
    
    def __init__(self, db_dsn: str = None):
        self.db_dsn = db_dsn
    
    def insert_definition(
        self,
        entry: Dict[str, Any],
        validate: bool = True,
        tenant_id: str = 'default'
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Insert a new workable definition.
        
        Args:
            entry: Definition dict (must have id, term, claim, scope, source_ldu_t_ids)
            validate: If True, validate entry before inserting
            tenant_id: Tenant identifier
        
        Returns:
            (success: bool, message: str, definition_id: Optional[str])
        """
        try:
            with get_connection(self.db_dsn) as conn:
                # Validate if requested
                if validate:
                    result = validate_ldu_w_full(entry, conn)
                    if not result.is_valid:
                        errors = result.to_dict()
                        return False, json.dumps(errors), None

                # Extract required fields
                definition_id = entry.get('id')
                if not definition_id:
                    return False, "Entry must have 'id' field", None

                term = entry.get('term')
                claim = entry.get('claim', {})
                source_ids = entry.get('source_ldu_t_ids', [])
                scope = entry.get('scope', {})
                gdu_shape = entry.get('gdu_shape', {})
                derivation = entry.get('derivation_method', 'unknown')
                status = entry.get('status', 'draft')
                metadata = entry.get('metadata', {})
                version = entry.get('version', 1)

                now = datetime.now(timezone.utc).isoformat()

                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO ldu_w_definitions (
                            id, universe, term, claim, source_ldu_t_ids,
                            gdu_shape, scope_domain, scope_culture, scope_community, scope_time_period,
                            derivation_method, status, metadata, version, created_at, updated_at,
                            tenant_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            definition_id,
                            'LDU-W',
                            term,
                            Json(claim),
                            source_ids,
                            Json(gdu_shape),
                            scope.get('domain'),
                            scope.get('culture'),
                            scope.get('community'),
                            scope.get('time_period'),
                            derivation,
                            status,
                            Json(metadata),
                            version,
                            now,
                            now,
                            tenant_id
                        )
                    )
                    cur.fetchone()

                conn.commit()
                return True, f"Definition inserted: {definition_id}", definition_id

        except psycopg2.Error as e:
            return False, f"Database error: {str(e)}", None
        except Exception as e:
            return False, f"Error: {str(e)}", None
    
    def update_metadata(
        self,
        definition_id: str,
        metadata_updates: Dict[str, Any],
        updated_by: str = 'system',
        tenant_id: str = 'default'
    ) -> Tuple[bool, str]:
        """
        Update metadata on existing definition (mutation allowed).
        
        Args:
            definition_id: LDU-W definition ID
            metadata_updates: Dict to merge into metadata
            updated_by: User/system identifier
            tenant_id: Tenant identifier
        
        Returns:
            (success: bool, message: str)
        """
        try:
            with get_connection(self.db_dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT metadata FROM ldu_w_definitions WHERE id = %s AND tenant_id = %s",
                        (definition_id, tenant_id)
                    )
                    row = cur.fetchone()
                    if not row:
                        return False, f"Definition not found: {definition_id}"

                    current_metadata = row[0] or {}
                    new_metadata = {**current_metadata, **metadata_updates}
                    new_metadata['last_updated_by'] = updated_by
                    new_metadata['last_update_time'] = datetime.now(timezone.utc).isoformat()

                    now = datetime.now(timezone.utc).isoformat()
                    cur.execute(
                        """
                        UPDATE ldu_w_definitions
                        SET metadata = %s, updated_at = %s
                        WHERE id = %s AND tenant_id = %s
                        """,
                        (Json(new_metadata), now, definition_id, tenant_id)
                    )

                    if cur.rowcount == 0:
                        return False, f"Definition not found or update failed: {definition_id}"

                conn.commit()
                return True, f"Metadata updated for {definition_id}"

        except psycopg2.Error as e:
            return False, f"Database error: {str(e)}"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def update_status(
        self,
        definition_id: str,
        new_status: str,
        updated_by: str = 'system',
        tenant_id: str = 'default'
    ) -> Tuple[bool, str]:
        """
        Update status on existing definition (workflow: draft → reviewed → ready_for_promotion).
        
        Args:
            definition_id: LDU-W definition ID
            new_status: One of: draft, reviewed, ready_for_promotion
            updated_by: User/system identifier
            tenant_id: Tenant identifier
        
        Returns:
            (success: bool, message: str)
        """
        valid_statuses = ['draft', 'reviewed', 'ready_for_promotion']
        if new_status not in valid_statuses:
            return False, f"Invalid status: {new_status}. Must be one of: {valid_statuses}"
        
        try:
            with get_connection(self.db_dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id FROM ldu_w_definitions WHERE id = %s AND tenant_id = %s",
                        (definition_id, tenant_id)
                    )
                    if not cur.fetchone():
                        return False, f"Definition not found: {definition_id}"

                    now = datetime.now(timezone.utc).isoformat()
                    cur.execute(
                        """
                        UPDATE ldu_w_definitions
                        SET status = %s, updated_at = %s
                        WHERE id = %s AND tenant_id = %s
                        """,
                        (new_status, now, definition_id, tenant_id)
                    )

                conn.commit()
                return True, f"Status updated to '{new_status}' for {definition_id}"

        except psycopg2.Error as e:
            return False, f"Database error: {str(e)}"
        except Exception as e:
            return False, f"Error: {str(e)}"
    
    def get_definition_by_id(
        self,
        definition_id: str,
        tenant_id: str = 'default'
    ) -> Optional[Dict[str, Any]]:
        """
        Retrieve definition by ID.
        
        Args:
            definition_id: LDU-W definition ID
            tenant_id: Tenant identifier
        
        Returns:
            Definition dict or None if not found
        """
        try:
            with get_connection(self.db_dsn) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT * FROM ldu_w_definitions
                        WHERE id = %s AND tenant_id = %s
                        """,
                        (definition_id, tenant_id)
                    )
                    return cur.fetchone()

        except Exception as e:
            print(f"Error retrieving definition: {str(e)}")
            return None
    
    def get_definitions_by_scope(
        self,
        domain: str,
        culture: str,
        community: str,
        time_period: Optional[str] = None,
        tenant_id: str = 'default'
    ) -> List[Dict[str, Any]]:
        """
        Query definitions by scope (uses composite index).
        
        Args:
            domain: Scope domain
            culture: Scope culture
            community: Scope community
            time_period: Optional time period (if None, ignores)
            tenant_id: Tenant identifier
        
        Returns:
            List of matching definitions
        """
        try:
            with get_connection(self.db_dsn) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    if time_period:
                        cur.execute(
                            """
                            SELECT * FROM ldu_w_definitions
                            WHERE scope_domain = %s
                              AND scope_culture = %s
                              AND scope_community = %s
                              AND scope_time_period = %s
                              AND tenant_id = %s
                            ORDER BY created_at DESC
                            """,
                            (domain, culture, community, time_period, tenant_id)
                        )
                    else:
                        cur.execute(
                            """
                            SELECT * FROM ldu_w_definitions
                            WHERE scope_domain = %s
                              AND scope_culture = %s
                              AND scope_community = %s
                              AND tenant_id = %s
                            ORDER BY created_at DESC
                            """,
                            (domain, culture, community, tenant_id)
                        )

                    return cur.fetchall() or []

        except Exception as e:
            print(f"Error querying by scope: {str(e)}")
            return []
    
    def get_definitions_by_ldu_t_source(
        self,
        ldu_t_entry_id: str,
        tenant_id: str = 'default'
    ) -> List[Dict[str, Any]]:
        """
        Reverse query: find all LDU-W definitions sourced from a given LDU-T entry.
        
        Uses GIN index on source_ldu_t_ids for efficiency.
        
        Args:
            ldu_t_entry_id: LDU-T entry ID to search for
            tenant_id: Tenant identifier
        
        Returns:
            List of definitions that cite this LDU-T entry
        """
        try:
            with get_connection(self.db_dsn) as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT * FROM ldu_w_definitions
                        WHERE source_ldu_t_ids @> %s
                          AND tenant_id = %s
                        ORDER BY created_at DESC
                        """,
                        ([ldu_t_entry_id], tenant_id)
                    )

                    return cur.fetchall() or []

        except Exception as e:
            print(f"Error querying by source: {str(e)}")
            return []
    
    def batch_insert(
        self,
        entries: List[Dict[str, Any]],
        derivation_method: str,
        ingestion_actor: str,
        tenant_id: str = 'default'
    ) -> Tuple[bool, str, Optional[str]]:
        """
        Batch insert definitions and create ingestion receipt.
        
        Args:
            entries: List of definition dicts
            derivation_method: How definitions were created (e.g., 'semantic_analysis_v1.0')
            ingestion_actor: User/system identifier
            tenant_id: Tenant identifier
        
        Returns:
            (success: bool, message: str, receipt_id: Optional[str])
        """
        receipt_id = None

        try:
            with get_connection(self.db_dsn) as conn:
                submitted = len(entries)
                accepted = 0
                rejected = 0
                rejected_details = []

                with conn.cursor() as cur:
                    for idx, entry in enumerate(entries):
                        validation = validate_ldu_w_full(entry, conn)
                        if not validation.is_valid:
                            rejected += 1
                            rejected_details.append({
                                'index': idx,
                                'id': entry.get('id', 'unknown'),
                                'errors': validation.to_dict()
                            })
                            continue

                        try:
                            definition_id = entry.get('id')
                            term = entry.get('term')
                            claim = entry.get('claim', {})
                            source_ids = entry.get('source_ldu_t_ids', [])
                            scope = entry.get('scope', {})
                            gdu_shape = entry.get('gdu_shape', {})
                            status = entry.get('status', 'draft')
                            metadata = entry.get('metadata', {})
                            version = entry.get('version', 1)

                            now = datetime.now(timezone.utc).isoformat()

                            cur.execute(
                                """
                                INSERT INTO ldu_w_definitions (
                                    id, universe, term, claim, source_ldu_t_ids,
                                    gdu_shape, scope_domain, scope_culture, scope_community, scope_time_period,
                                    derivation_method, status, metadata, version, created_at, updated_at,
                                    tenant_id
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                (
                                    definition_id,
                                    'LDU-W',
                                    term,
                                    Json(claim),
                                    source_ids,
                                    Json(gdu_shape),
                                    scope.get('domain'),
                                    scope.get('culture'),
                                    scope.get('community'),
                                    scope.get('time_period'),
                                    derivation_method,
                                    status,
                                    Json(metadata),
                                    version,
                                    now,
                                    now,
                                    tenant_id
                                )
                            )
                            accepted += 1

                        except psycopg2.Error as e:
                            rejected += 1
                            rejected_details.append({
                                'index': idx,
                                'id': entry.get('id', 'unknown'),
                                'error': str(e)
                            })

                    ingestion_ts = datetime.now(timezone.utc).isoformat()
                    receipt_id = f"receipt_{ingestion_ts.replace(':', '').replace('-', '')}"

                    cur.execute(
                        """
                        INSERT INTO ldu_w_ingestion_receipts (
                            ingestion_id, ingestion_timestamp, ingestion_method,
                            ingestion_actor, definitions_submitted, definitions_accepted,
                            definitions_rejected, rejected_details, tenant_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            receipt_id,
                            ingestion_ts,
                            'batch_insert',
                            ingestion_actor,
                            submitted,
                            accepted,
                            rejected,
                            Json({'details': rejected_details}),
                            tenant_id
                        )
                    )

                conn.commit()
                msg = f"Batch insert: {submitted} submitted, {accepted} accepted, {rejected} rejected"
                return True, msg, receipt_id

        except Exception as e:
            return False, f"Batch insert error: {str(e)}", None
