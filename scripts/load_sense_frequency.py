#!/usr/bin/env python3
"""Load the sense-frequency pack into form_concept_bindings.binding_evidence.

The dominant-sense prior lives on the BINDING (per-lemma), not the concept: a synset
is sense 1 for one lemma and sense 5 for another. For each (lemma, synset) row in the
pack, match lemma → form_nodes.surface_form_normalized and synset_id → concept
type_evidence 'sense_id', and stamp the binding with {sense_rank, n_senses, freq},
where freq = 1/rank (rank 1 = most frequent = 1.0). The collapse equation reads freq
as the `frequency` evidence axis.

Idempotent (merges into binding_evidence; re-run overwrites the same keys). Read the
pack from the mounted libraries path by default.

  docker exec -e ENKI_DB_DSN=...enki_harness app-api-1 \
    python /app/scripts/load_sense_frequency.py [PACK.json]
"""
from __future__ import annotations
import csv
import io
import json
import sys

sys.path.insert(0, "/app")
from storage.postgres.db import get_connection

DEFAULT_PACK = "/app/libraries/sense_frequency/definitions/sense_frequency.json"


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PACK
    tenant = sys.argv[2] if len(sys.argv) > 2 else "default"
    rows = json.load(open(path))["content"]["ranks"]

    buf = io.StringIO()
    wtr = csv.writer(buf)
    for r in rows:
        freq = round(1.0 / r["rank"], 6)
        wtr.writerow([r["lemma"], r["synset_id"], r["rank"], r["n_senses"], freq])
    buf.seek(0)

    with get_connection() as conn, conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE _sf (lemma text, sid text, rank int, "
                    "n int, freq real) ON COMMIT DROP")
        cur.copy_expert("COPY _sf FROM STDIN WITH (FORMAT csv)", buf)
        cur.execute("ANALYZE _sf")
        cur.execute("""
            UPDATE form_concept_bindings b
               SET binding_evidence =
                   COALESCE(b.binding_evidence, '{}'::jsonb)
                   || jsonb_build_object('sense_rank', s.rank,
                                         'n_senses', s.n,
                                         'freq', s.freq)
              FROM _sf s, form_nodes f, concept_nodes c
             WHERE b.form_id = f.form_id
               AND b.concept_id = c.concept_id
               AND f.tenant_id = %s
               AND f.surface_form_normalized = s.lemma
               AND c.type_evidence->>'sense_id' = s.sid
               AND c.subtype_path LIKE 'lexical.%%'
        """, (tenant,))
        n = cur.rowcount
        conn.commit()
    print(f"updated {n} bindings with sense frequency (from {len(rows)} pack rows)")


if __name__ == "__main__":
    main()
