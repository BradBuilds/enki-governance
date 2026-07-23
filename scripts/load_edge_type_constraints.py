#!/usr/bin/env python3
"""
Load edge type constraints from edgevocab into gdu_type_constraints.

Source: libraries/edgevocab/definitions/edgevocab.edge_types.json
Target: gdu_type_constraints (one row per declared edge type) +
        definitions (one row per edge_type_def with base-60 address)

Idempotent. Safe to re-run after edgevocab changes; upserts existing
rows and inserts new ones. Definitions get permanent addresses; if an
edge type is removed from edgevocab, its definition is marked
status='deprecated' but kept (the address is permanent).

Run inside the api container:
    docker exec app-api-1 python /app/scripts/load_edge_type_constraints.py
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from core.gdu.address import assign_address
from storage.postgres.db import get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("load_edge_constraints")

EDGEVOCAB_PATH = Path("/app/libraries/edgevocab/definitions/edgevocab.edge_types.json")
PACK_PATH      = Path("/app/libraries/edgevocab/pack.json")


def load_pack_version() -> str:
    if PACK_PATH.exists():
        meta = json.loads(PACK_PATH.read_text())
        return meta.get("version", "unknown")
    return "unknown"


def load_edge_types() -> dict[str, dict]:
    if not EDGEVOCAB_PATH.exists():
        log.error("edgevocab definitions missing: %s", EDGEVOCAB_PATH)
        return {}
    data = json.loads(EDGEVOCAB_PATH.read_text())
    return data.get("content", {}).get("edges", {}) or {}


def upsert_constraint(conn, edge_type: str, spec: dict, version: str) -> tuple[bool, str | None]:
    """Returns (is_new, base60_address)."""
    src_types = spec.get("source_types") or []
    dst_types = spec.get("target_types") or []
    sub_roles = spec.get("sub_roles") or []
    label = spec.get("label")
    desc  = spec.get("description")
    is_symmetric = bool(spec.get("symmetric", False))
    fires     = bool(spec.get("fires_automatically", True))
    inverse   = spec.get("inverse")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT constraint_id, base60_address FROM gdu_type_constraints WHERE edge_type = %s",
            (edge_type,),
        )
        row = cur.fetchone()
        if row:
            cid, addr = row
            cur.execute(
                """
                UPDATE gdu_type_constraints
                   SET label = %s, description = %s,
                       source_types = %s, target_types = %s,
                       sub_roles = %s,
                       is_symmetric = %s, fires_automatically = %s,
                       inverse_edge_type = %s,
                       pack_source = 'edgevocab', pack_version = %s,
                       updated_at = NOW()
                 WHERE constraint_id = %s
                """,
                (label, desc, src_types, dst_types, sub_roles,
                 is_symmetric, fires, inverse, version, cid),
            )
            return False, addr

        # New constraint — mint a definition address
        addr = assign_address(conn, "definition", "edge_type_def")
        cur.execute(
            """
            INSERT INTO gdu_type_constraints
                (edge_type, label, description,
                 source_types, target_types, sub_roles,
                 is_symmetric, fires_automatically, inverse_edge_type,
                 pack_source, pack_version, base60_address)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'edgevocab', %s, %s)
            """,
            (edge_type, label, desc, src_types, dst_types, sub_roles,
             is_symmetric, fires, inverse, version, addr),
        )
        return True, addr


def upsert_definition(conn, edge_type: str, spec: dict, addr: str, version: str) -> bool:
    """Mirror the edge type into the definitions table as edge_type_def."""
    payload = {
        "edge_type":   edge_type,
        "label":       spec.get("label"),
        "source_types": spec.get("source_types") or [],
        "target_types": spec.get("target_types") or [],
        "sub_roles":    spec.get("sub_roles") or [],
        "is_symmetric": bool(spec.get("symmetric", False)),
        "fires_automatically": bool(spec.get("fires_automatically", True)),
        "inverse":      spec.get("inverse"),
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT definition_id FROM definitions
             WHERE definition_kind = 'edge_type_def'
               AND canonical_name = %s
            """,
            (edge_type,),
        )
        row = cur.fetchone()
        if row:
            cur.execute(
                """
                UPDATE definitions
                   SET payload = %s, last_seen = NOW(),
                       pack_version = %s
                 WHERE definition_id = %s
                """,
                (json.dumps(payload), version, row[0]),
            )
            return False
        cur.execute(
            """
            INSERT INTO definitions
                (definition_kind, base60_address, canonical_name, description,
                 pack_source, pack_version, payload, status)
            VALUES ('edge_type_def', %s, %s, %s, 'edgevocab', %s, %s, 'active')
            RETURNING definition_id
            """,
            (addr, edge_type, spec.get("description"), version, json.dumps(payload)),
        )
        def_id = cur.fetchone()[0]
        # Cross-link
        cur.execute(
            "UPDATE gdu_type_constraints SET definition_id = %s WHERE edge_type = %s",
            (def_id, edge_type),
        )
        return True


def main() -> int:
    version = load_pack_version()
    edges = load_edge_types()
    if not edges:
        log.error("no edges to load")
        return 1

    log.info("Loading %d edge types from edgevocab v%s", len(edges), version)
    stats = {"new_constraints": 0, "updated_constraints": 0,
             "new_definitions": 0, "updated_definitions": 0}

    for edge_type, spec in edges.items():
        try:
            with get_connection() as conn:
                is_new, addr = upsert_constraint(conn, edge_type, spec, version)
                stats["new_constraints" if is_new else "updated_constraints"] += 1
                def_is_new = upsert_definition(conn, edge_type, spec, addr, version)
                stats["new_definitions" if def_is_new else "updated_definitions"] += 1
                conn.commit()
        except Exception:
            log.exception("failed for edge_type=%s", edge_type)

    log.info("Done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
