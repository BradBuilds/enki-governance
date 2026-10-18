"""
Entity ↔ Node Sync — keeps contacts, assets, and knowledge nodes in sync.

When a contact or asset is created/updated, this module ensures a corresponding
knowledge node exists and is updated. When a knowledge node is enriched with
entity data, this module can create/update the contact or asset.

This is the bridge between the dossier system (contacts/assets) and the
intelligence system (knowledge graph).

Sync directions:
  Contact/Asset → Node:  sync_contact_to_node(), sync_asset_to_node()
  Node → Contact/Asset:  sync_node_to_contact(), sync_node_to_asset()
  Batch:                 sync_all_entities()
"""

from __future__ import annotations

import json
import uuid
import logging
from typing import Optional

from storage.postgres.db import get_connection
from core.gdu.enforce import admit_node as gdu_admit_node

log = logging.getLogger("enki.entity_sync")


def _sync_via_gateway(tenant_id: str, category: str, subtype: str,
                      title: str, description: str, node_id: str) -> Optional[str]:
    """Admit a sync node through the GDU gateway. Returns node_id on success."""
    try:
        result = gdu_admit_node(
            tenant_id=tenant_id,
            category=category,
            subtype=subtype,
            title=title[:200],
            description=description[:500] if description else "",
            signal_rid=0,
            collapse_key=node_id,
        )
        return result.node_id if result.action in ("admit", "collapse") else None
    except Exception:
        log.warning("Gateway sync skipped for %s (secondary INSERT will not be gdu_canonical)", node_id)
        return None


# ─── Contact → Node ─────────────────────────────────────────────────────────

def sync_contact_to_node(contact_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[str]:
    """Ensure a knowledge node exists for this contact and is up to date."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT contact_id, contact_type, primary_name, aliases, email, phone,
                       address, role_title, notes, threat_level, node_id,
                       date_of_birth, nationality, occupation, employer,
                       last_known_location, last_known_lat, last_known_lon,
                       registration_number, registration_country, industry, website
                FROM contacts WHERE contact_id = %s AND tenant_id = %s
            """, (contact_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None

    contact = {
        "contact_id": row[0], "contact_type": row[1], "primary_name": row[2],
        "aliases": row[3] or [], "email": row[4], "phone": row[5],
        "address": row[6], "role_title": row[7], "notes": row[8],
        "threat_level": row[9], "node_id": row[10],
        "date_of_birth": row[11], "nationality": row[12],
        "occupation": row[13], "employer": row[14],
        "last_known_location": row[15], "last_known_lat": row[16], "last_known_lon": row[17],
        "registration_number": row[18], "registration_country": row[19],
        "industry": row[20], "website": row[21],
    }

    # Map contact_type to entity_type
    entity_type = {"person": "person", "company": "organization", "address": "facility"}.get(
        contact["contact_type"], "person"
    )

    # Build identifiers
    identifiers = {}
    if contact["email"]:
        identifiers["email"] = contact["email"]
    if contact["phone"]:
        identifiers["phone"] = contact["phone"]
    if contact["registration_number"]:
        identifiers["registration_number"] = contact["registration_number"]

    # Build description
    desc_parts = []
    if contact["role_title"]:
        desc_parts.append(f"Role: {contact['role_title']}")
    if contact["employer"]:
        desc_parts.append(f"Employer: {contact['employer']}")
    if contact["occupation"]:
        desc_parts.append(f"Occupation: {contact['occupation']}")
    if contact["nationality"]:
        desc_parts.append(f"Nationality: {contact['nationality']}")
    if contact["address"]:
        desc_parts.append(f"Address: {contact['address']}")
    if contact["notes"]:
        desc_parts.append(contact["notes"][:300])
    description = ". ".join(desc_parts) if desc_parts else None

    # Determine epistemic type based on data quality
    epistemic_type = "fact" if contact["threat_level"] != "none" or contact["notes"] else "observation"

    node_id = contact["node_id"]

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if node_id:
                # Update existing node
                cur.execute("""
                    UPDATE knowledge_nodes SET
                        title = %s, description = %s, epistemic_type = %s,
                        entity_type = %s, entity_name = %s, entity_identifiers = %s,
                        latitude = %s, longitude = %s, place_name = %s,
                        source_authority = 'trusted', updated_at = NOW()
                    WHERE node_id = %s AND tenant_id = %s
                """, (
                    contact["primary_name"], description, epistemic_type,
                    entity_type, contact["primary_name"], json.dumps(identifiers),
                    contact["last_known_lat"], contact["last_known_lon"],
                    contact["last_known_location"],
                    node_id, tenant_id,
                ))
            else:
                # Create new node via GDU gateway
                node_id = f"node-{uuid.uuid4().hex[:12]}"
                _sync_via_gateway(tenant_id, "entity", entity_type or "person",
                                  contact["primary_name"], description, node_id)
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, project_id, tenant_id, title, description,
                         epistemic_type, entity_type, entity_name, entity_identifiers,
                         latitude, longitude, place_name,
                         source_authority, confidence, enrichment_status,
                         gdu_canonical)
                    VALUES (%s, '', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                            'trusted', 0.8, 'pending', FALSE)
                    ON CONFLICT (node_id) DO UPDATE SET
                        title = EXCLUDED.title, description = EXCLUDED.description,
                        updated_at = NOW()
                """, (
                    node_id, tenant_id, contact["primary_name"], description,
                    epistemic_type, entity_type, contact["primary_name"],
                    json.dumps(identifiers),
                    contact["last_known_lat"], contact["last_known_lon"],
                    contact["last_known_location"],
                ))
                # Link back
                cur.execute("""
                    UPDATE contacts SET node_id = %s WHERE contact_id = %s AND tenant_id = %s
                """, (node_id, contact_id, tenant_id))

            conn.commit()

    return node_id


# ─── Asset → Node ────────────────────────────────────────────────────────────

def sync_asset_to_node(asset_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[str]:
    """Ensure a knowledge node exists for this asset and is up to date."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT asset_id, display_name, description, asset_category, status,
                       serial_number, latitude, longitude, node_id,
                       vehicle_make, vehicle_model, vehicle_year, vehicle_vin,
                       vehicle_plate, vehicle_color,
                       vessel_mmsi, vessel_imo, vessel_callsign, vessel_flag_state,
                       vessel_type, vessel_owner,
                       aircraft_registration, aircraft_icao_hex, aircraft_model, aircraft_owner,
                       property_address, property_type, estimated_value
                FROM assets WHERE asset_id = %s AND tenant_id = %s
            """, (asset_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None

    asset = {
        "asset_id": row[0], "display_name": row[1], "description": row[2],
        "asset_category": row[3], "status": row[4], "serial_number": row[5],
        "latitude": row[6], "longitude": row[7], "node_id": row[8],
        "vehicle_make": row[9], "vehicle_model": row[10], "vehicle_year": row[11],
        "vehicle_vin": row[12], "vehicle_plate": row[13], "vehicle_color": row[14],
        "vessel_mmsi": row[15], "vessel_imo": row[16], "vessel_callsign": row[17],
        "vessel_flag_state": row[18], "vessel_type": row[19], "vessel_owner": row[20],
        "aircraft_registration": row[21], "aircraft_icao_hex": row[22],
        "aircraft_model": row[23], "aircraft_owner": row[24],
        "property_address": row[25], "property_type": row[26], "estimated_value": row[27],
    }

    # Map category to entity_type
    entity_type_map = {
        "vehicle": "vehicle", "vessel": "vessel", "aircraft": "aircraft",
        "real_estate": "facility", "electronics": "device", "equipment": "device",
    }
    entity_type = entity_type_map.get(asset["asset_category"], "device")

    # Build identifiers per category
    identifiers = {}
    if asset["serial_number"]:
        identifiers["serial"] = asset["serial_number"]
    if asset["vehicle_vin"]:
        identifiers["vin"] = asset["vehicle_vin"]
    if asset["vehicle_plate"]:
        identifiers["plate"] = asset["vehicle_plate"]
    if asset["vessel_mmsi"]:
        identifiers["mmsi"] = asset["vessel_mmsi"]
    if asset["vessel_imo"]:
        identifiers["imo"] = asset["vessel_imo"]
    if asset["vessel_callsign"]:
        identifiers["callsign"] = asset["vessel_callsign"]
    if asset["aircraft_registration"]:
        identifiers["registration"] = asset["aircraft_registration"]
    if asset["aircraft_icao_hex"]:
        identifiers["icao_hex"] = asset["aircraft_icao_hex"]

    # Build description
    desc_parts = []
    if asset["description"]:
        desc_parts.append(asset["description"][:300])
    if asset["vehicle_make"]:
        desc_parts.append(f"{asset['vehicle_year'] or ''} {asset['vehicle_make']} {asset['vehicle_model'] or ''}".strip())
    if asset["vessel_type"]:
        desc_parts.append(f"Vessel type: {asset['vessel_type']}, flag: {asset['vessel_flag_state'] or 'unknown'}")
    if asset["aircraft_model"]:
        desc_parts.append(f"Aircraft: {asset['aircraft_model']}")
    if asset["property_address"]:
        desc_parts.append(f"Property: {asset['property_address']}")
    if asset["estimated_value"]:
        desc_parts.append(f"Est. value: ${asset['estimated_value']:,.2f}")
    description = ". ".join(desc_parts) if desc_parts else None

    # Place name
    place_name = asset.get("property_address") or asset.get("vessel_flag_state")

    node_id = asset["node_id"]

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            if node_id:
                cur.execute("""
                    UPDATE knowledge_nodes SET
                        title = %s, description = %s,
                        entity_type = %s, entity_name = %s, entity_identifiers = %s,
                        latitude = %s, longitude = %s, place_name = %s,
                        source_authority = 'trusted', updated_at = NOW()
                    WHERE node_id = %s AND tenant_id = %s
                """, (
                    asset["display_name"], description,
                    entity_type, asset["display_name"], json.dumps(identifiers),
                    asset["latitude"], asset["longitude"], place_name,
                    node_id, tenant_id,
                ))
            else:
                node_id = f"node-{uuid.uuid4().hex[:12]}"
                _sync_via_gateway(tenant_id, "entity", entity_type or "vehicle",
                                  asset["display_name"], description or "", node_id)
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, project_id, tenant_id, title, description,
                         epistemic_type, entity_type, entity_name, entity_identifiers,
                         latitude, longitude, place_name,
                         source_authority, confidence, enrichment_status,
                         gdu_canonical)
                    VALUES (%s, '', %s, %s, %s, 'fact', %s, %s, %s, %s, %s, %s,
                            'trusted', 0.85, 'pending', FALSE)
                    ON CONFLICT (node_id) DO UPDATE SET
                        title = EXCLUDED.title, description = EXCLUDED.description,
                        updated_at = NOW()
                """, (
                    node_id, tenant_id, asset["display_name"], description,
                    entity_type, asset["display_name"], json.dumps(identifiers),
                    asset["latitude"], asset["longitude"], place_name,
                ))
                cur.execute("""
                    UPDATE assets SET node_id = %s WHERE asset_id = %s AND tenant_id = %s
                """, (node_id, asset_id, tenant_id))

            conn.commit()

    return node_id


# ─── Financial Account → Node ────────────────────────────────────────────────

def sync_account_to_node(account_id: str, tenant_id: str, dsn: Optional[str] = None) -> Optional[str]:
    """Ensure a knowledge node exists for this financial account."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT account_id, display_name, institution, account_type,
                       last_four, currency, current_balance
                FROM financial_accounts WHERE account_id = %s AND tenant_id = %s
            """, (account_id, tenant_id))
            row = cur.fetchone()
            if not row:
                return None

    node_id = f"node-{uuid.uuid4().hex[:12]}"
    identifiers = {"institution": row[2] or "", "last_four": row[4] or "", "type": row[3] or ""}
    description = f"{row[2] or 'Account'} {row[3] or ''} ****{row[4] or ''}. Balance: {row[6] or 0} {row[5] or 'USD'}"

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Check if node already exists for this account
            cur.execute("""
                SELECT node_id FROM knowledge_nodes
                WHERE tenant_id = %s AND entity_type = 'account'
                  AND entity_identifiers->>'last_four' = %s
                  AND entity_identifiers->>'institution' = %s
                LIMIT 1
            """, (tenant_id, row[4] or "", row[2] or ""))
            existing = cur.fetchone()
            if existing:
                node_id = existing[0]
                cur.execute("""
                    UPDATE knowledge_nodes SET description = %s, updated_at = NOW()
                    WHERE node_id = %s
                """, (description, node_id))
            else:
                _sync_via_gateway(tenant_id, "entity", "account",
                                  row[1] or f"Account ****{row[4]}",
                                  description, node_id)
                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, project_id, tenant_id, title, description,
                         epistemic_type, entity_type, entity_name, entity_identifiers,
                         source_authority, confidence, enrichment_status,
                         gdu_canonical)
                    VALUES (%s, '', %s, %s, %s, 'fact', 'account', %s, %s,
                            'verified', 0.95, 'complete', FALSE)
                    ON CONFLICT (node_id) DO UPDATE SET
                        description = EXCLUDED.description,
                        updated_at = NOW()
                """, (node_id, tenant_id, row[1] or f"Account ****{row[4]}",
                      description, row[1], json.dumps(identifiers)))
            conn.commit()

    return node_id


# ─── Batch Sync ──────────────────────────────────────────────────────────────

def sync_all_entities(tenant_id: str, dsn: Optional[str] = None) -> dict:
    """Sync all contacts, assets, and accounts to knowledge nodes."""
    results = {"contacts": 0, "assets": 0, "accounts": 0}

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Contacts without nodes
            cur.execute("""
                SELECT contact_id FROM contacts
                WHERE tenant_id = %s AND (node_id IS NULL OR node_id = '')
            """, (tenant_id,))
            for (cid,) in cur.fetchall():
                if sync_contact_to_node(cid, tenant_id, dsn):
                    results["contacts"] += 1

            # Assets without nodes
            cur.execute("""
                SELECT asset_id FROM assets
                WHERE tenant_id = %s AND (node_id IS NULL OR node_id = '')
            """, (tenant_id,))
            for (aid,) in cur.fetchall():
                if sync_asset_to_node(aid, tenant_id, dsn):
                    results["assets"] += 1

            # Accounts (always sync since they don't have node_id column)
            cur.execute("""
                SELECT account_id FROM financial_accounts WHERE tenant_id = %s
            """, (tenant_id,))
            for (fid,) in cur.fetchall():
                if sync_account_to_node(fid, tenant_id, dsn):
                    results["accounts"] += 1

    return results
