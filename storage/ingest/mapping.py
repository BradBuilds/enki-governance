"""
ObservabilityEvent to AuditEvent mapping per Phase 2 Appendix A.
"""

from typing import Dict, Any, Tuple


def map_observability_to_audit(obs_event: dict, tenant_id: str) -> Tuple[Dict[str, Any], str]:
    """
    Map ObservabilityEvent to AuditEvent structure.
    
    Implements field mapping from the event emission spec:
    - Direct field copies: event_id, event_type, timestamp, severity, scope
    - Special handling: sequence → metadata._enki_observability_sequence
    - Tenant defaulting: use ingestion tenant_id if not present
    
    Args:
        obs_event: Parsed ObservabilityEvent dict from JSONL
        tenant_id: Tenant context for ingestion (fallback if not in event)
    
    Returns:
        Tuple of (AuditEvent dict for Phase 0 validation, tenant_id for DB context)
        
    Note:
        tenant_id is returned separately because it's a ledger partition key (DB column),
        not an event field subject to schema validation.
    
    Raises:
        KeyError: If required ObservabilityEvent fields missing
    """
    # Extract sequence for special handling per Appendix A
    sequence = obs_event.get('sequence')
    
    # Get metadata or create empty dict
    metadata = obs_event.get('metadata')
    if metadata is None:
        metadata = {}
    else:
        # Copy to avoid mutating original
        metadata = dict(metadata)
    
    # Store sequence in metadata per Appendix A rule:
    # "sequence MUST map to AuditEvent.metadata._enki_observability_sequence"
    if sequence is not None:
        metadata['_enki_observability_sequence'] = sequence
    
    # Build AuditEvent structure with MUST fields
    audit_event = {
        'event_id': obs_event['event_id'],
        'event_type': obs_event['event_type'],
        'timestamp': obs_event['timestamp'],
        'severity': obs_event['severity'],
        'scope': obs_event['scope'],
        'metadata': metadata,
    }
    
    # Add optional fields if present and non-null
    optional_fields = [
        'actor_id', 'assertion_id', 'pack_id', 
        'node_id', 'target_id', 'session_id', 'correlation_id'
    ]
    
    for field in optional_fields:
        if field in obs_event and obs_event[field] is not None:
            audit_event[field] = obs_event[field]
    
    # Extract tenant_id from event or use ingestion context
    # tenant_id is returned separately as it's a DB partition key, not a schema field
    event_tenant_id = obs_event.get('tenant_id', tenant_id)
    
    return audit_event, event_tenant_id
