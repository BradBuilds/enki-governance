#!/usr/bin/env python3
"""
Load the predicate dictionary from libraries/edgevocab/definitions/
edgevocab.predicate_seeds.json into the predicate_dictionary table.

Each row gets a base-60 address (form/verb category — we reuse the
verb subtype since lemmas of verbs ARE form/verb instances). Idempotent;
upserts on (canonical_lemma, edge_type).

Run inside the api container:
    docker exec app-api-1 python /app/scripts/load_predicate_seeds.py
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
log = logging.getLogger("load_predicate_seeds")

SEED_PATH = Path("/app/libraries/edgevocab/definitions/edgevocab.predicate_seeds.json")


def main() -> int:
    if not SEED_PATH.exists():
        log.error("seed file missing: %s", SEED_PATH)
        return 1
    seeds = json.loads(SEED_PATH.read_text()).get("seeds") or []
    log.info("Loading %d predicate seeds", len(seeds))

    stats = {"inserted": 0, "updated": 0, "skipped_edge_missing": 0,
             "errors": 0, "definitions_emitted": 0}

    for s in seeds:
        lemma = s["lemma"]
        edge_type = s["edge_type"]
        confidence = float(s.get("confidence") or 0.7)
        try:
            with get_connection() as conn:
                with conn.cursor() as cur:
                    # Verify edge_type exists in the constraint table
                    cur.execute(
                        "SELECT 1 FROM gdu_type_constraints WHERE edge_type = %s",
                        (edge_type,),
                    )
                    if not cur.fetchone():
                        stats["skipped_edge_missing"] += 1
                        log.debug("skipping unknown edge_type=%s for lemma=%s", edge_type, lemma)
                        continue

                    # Upsert into predicate_dictionary
                    cur.execute(
                        """
                        SELECT entry_id, base60_address FROM predicate_dictionary
                         WHERE canonical_lemma = %s AND edge_type = %s
                        """,
                        (lemma, edge_type),
                    )
                    row = cur.fetchone()
                    if row:
                        cur.execute(
                            """
                            UPDATE predicate_dictionary
                               SET confidence = %s, last_seen = NOW(), source = 'seed_v1'
                             WHERE entry_id = %s
                            """,
                            (confidence, row[0]),
                        )
                        stats["updated"] += 1
                    else:
                        # Mint address: form/verb subtype
                        addr = assign_address(conn, "form", "verb")
                        cur.execute(
                            """
                            INSERT INTO predicate_dictionary
                                (canonical_lemma, edge_type, base60_address,
                                 confidence, observation_count, source)
                            VALUES (%s, %s, %s, %s, 0, 'seed_v1')
                            """,
                            (lemma, edge_type, addr, confidence),
                        )
                        stats["inserted"] += 1

                        # Emit lemma_concept_def linking lemma → edge_type_def
                        cur.execute(
                            """
                            SELECT definition_id FROM definitions
                             WHERE definition_kind='edge_type_def' AND canonical_name=%s
                            """,
                            (edge_type,),
                        )
                        edge_def = cur.fetchone()
                        if edge_def:
                            cur.execute(
                                """
                                SELECT definition_id FROM definitions
                                 WHERE definition_kind='lemma_concept_def'
                                   AND canonical_name = %s
                                """,
                                (f"verb:{lemma}",),
                            )
                            if not cur.fetchone():
                                def_addr = assign_address(conn, "definition", "lemma_concept_def")
                                cur.execute(
                                    """
                                    INSERT INTO definitions
                                        (definition_kind, base60_address, canonical_name,
                                         description, pack_source, pack_version, payload, status)
                                    VALUES ('lemma_concept_def', %s, %s,
                                            %s, 'edgevocab.predicate_seeds', '1.0',
                                            %s, 'active')
                                    """,
                                    (def_addr, f"verb:{lemma}",
                                     f"Verb lemma '{lemma}' maps to edge type '{edge_type}'.",
                                     json.dumps({"lemma": lemma, "edge_type": edge_type,
                                                  "confidence": confidence,
                                                  "predicate_address": addr,
                                                  "edge_type_def_id": edge_def[0]})),
                                )
                                stats["definitions_emitted"] += 1
                conn.commit()
        except Exception:
            log.exception("seed failed: %s", s)
            stats["errors"] += 1

    log.info("Done: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
