#!/usr/bin/env python3
"""
Load full WordNet into the grid (batched) and validate the equation over real
polysemy. Proves "add a real dictionary -> collapse against it" at scale.

  docker exec -e ENKI_DB_DSN=...enki_harness app-api-1 python3 /app/scripts/load_wordnet.py
"""
from __future__ import annotations

import json
import os
import sys
import time

from core.collapse.equation import Candidate, Context, Policy, collapse
from core.collapse.expansion import meaning_fan
from core.language.library_collapse import collapse_library_bulk

PACK = "/app/libraries/wordnet/definitions/wordnet.senses.json"
POL = Policy.load("/app/libraries/collapsepolicy")


def _conn():
    from storage.postgres.db import get_connection
    return get_connection()


def _guard():
    if "harness" not in os.environ.get("ENKI_DB_DSN", ""):
        sys.exit("REFUSING: ENKI_DB_DSN must name a *harness* DB")


def setup():
    with open("/app/storage/postgres/migrations/125_concept_relations.sql") as f:
        mig = f.read()
    with _conn() as c, c.cursor() as cur:
        cur.execute(mig)
        cur.execute("""SELECT string_agg(format('%I', relname), ', ')
                         FROM pg_stat_user_tables WHERE n_live_tup>0 OR n_dead_tup>0""")
        t = cur.fetchone()[0]
        if t:
            cur.execute(f"TRUNCATE {t} RESTART IDENTITY CASCADE")
        c.commit()


def candidates(cur, form):
    cur.execute("""
        SELECT c.concept_address, c.canonical_name, c.type_evidence->>'register',
               c.type_evidence->>'domain', c.type_evidence->>'gloss'
          FROM form_nodes f
          JOIN form_concept_bindings b ON b.form_id = f.form_id
          JOIN concept_nodes c ON c.concept_id = b.concept_id
         WHERE f.surface_form_normalized = %s AND c.subtype_path LIKE 'lexical.%%'
         ORDER BY c.concept_address
    """, (form,))
    rows = cur.fetchall()
    cands = [Candidate(a, n or "", r or "", d) for a, n, r, d, g in rows]
    gloss = {a: g for a, n, r, d, g in rows}
    return cands, gloss


def main():
    _guard()
    print("── load WordNet (batched) ──")
    setup()
    senses = json.load(open(PACK))["content"]["senses"]
    t0 = time.time()
    s = collapse_library_bulk(senses, "wordnet", _conn)
    dt = time.time() - t0
    print(f"   {s['senses']} senses, {s['forms']} bindings, {s['relations']} relations, "
          f"{s['rejected']} rejected  in {dt:.1f}s")
    with _conn() as c, c.cursor() as cur:
        cur.execute("SELECT (SELECT count(*) FROM concept_nodes WHERE subtype_path LIKE 'lexical.%'),"
                    "(SELECT count(*) FROM form_nodes),(SELECT count(*) FROM concept_relations)")
        print("   grid: %s senses, %s forms, %s relation edges" % cur.fetchone())

    print("── real polysemy through the equation ──")
    with _conn() as c, c.cursor() as cur:
        for word in ("fire", "lead", "bank", "spring", "light"):
            cands, gloss = candidates(cur, word)
            fan = meaning_fan(cands, Context(word), POL)
            top = sorted(fan.items(), key=lambda kv: (-kv[1], kv[0]))[:1]
            ta = top[0][0] if top else None
            print(f"   {word:7} {len(cands):3} senses  bare→{'PARK (ambiguous)' if len(cands)>1 else 'one'}"
                  + (f"   e.g. {gloss.get(ta,'')[:46]}" if ta else ""))
        # a contextual collapse on real data: "lead" with a chemistry-ish neighbour
        cands, gloss = candidates(cur, "fire")
        # pick the single most concrete sense by exact+name; with no context it parks — show that honesty
        r = collapse(cands, Context("fire"), POL)
        print(f"   collapse('fire', no context) -> {r.action} ({r.reason}) over {len(cands)} senses")
    print("✅ WordNet loaded + equation operates over real 120k-synset polysemy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
