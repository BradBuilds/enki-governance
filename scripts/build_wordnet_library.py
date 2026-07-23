#!/usr/bin/env python3
"""
Build the `wordnet` lexical library pack from Open English WordNet.

WordNet → the lexical-library schema (the same one example_lexicon uses):
  - each SYNSET is one concept (one sense) — its stable id (oewn-NNNNNNNN-p) is the
    library sense_id → deterministic base-60 address
  - the synset's LEMMAS become the forms that bind to it (synonymy = many forms,
    one concept — no synonym edges needed)
  - cross-synset relations (hypernym/hyponym/meronym/holonym) become relation edges
    — the expansion graph

Run with the wn venv (the package isn't a runtime dep — conversion is one-time):
  /tmp/wnvenv/bin/python scripts/build_wordnet_library.py  [LEX] [OUT.json] [LIMIT]

The output senses JSON is *generated data* (reproducible from this converter + the
pinned OEWN version), so it's cached/scp'd, not committed — like refdatapack.
"""
import json
import sys

import wn

POS = {"n": "noun", "v": "verb", "a": "adjective", "s": "adjective", "r": "adverb"}
# wn relation name → our governed rel_type (others dropped)
REL = {
    "hypernym": "hypernym", "hyponym": "hyponym",
    "mero_part": "meronym", "mero_member": "meronym", "mero_substance": "meronym",
    "holo_part": "holonym", "holo_member": "holonym", "holo_substance": "holonym",
    "antonym": "antonym",
}


def main():
    lex = sys.argv[1] if len(sys.argv) > 1 else "oewn:2023"
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/wordnet.senses.json"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    w = wn.Wordnet(lex)
    senses = []
    n = 0
    for s in w.synsets():
        lemmas = s.lemmas()
        if not lemmas:
            continue
        rels = []
        try:
            for rname, targets in s.relations().items():
                rt = REL.get(rname)
                if not rt:
                    continue
                for t in targets:
                    rels.append({"rel": rt, "sense_id": t.id})
        except Exception:
            pass
        senses.append({
            "sense_id": s.id,
            "lemma": lemmas[0],
            "pos": POS.get(s.pos, "other"),
            "register": "literal",
            "domain": (s.lexicographer_file() if hasattr(s, "lexicographer_file") else None),
            "gloss": (s.definition() or "")[:500],
            "forms": sorted(set(lemmas)),
            "relations": rels,
        })
        n += 1
        if limit and n >= limit:
            break

    doc = {
        "id": "wordnet.senses", "type": "lexical_senses",
        "version": lex.replace(":", "-"),
        "description": f"Open English WordNet ({lex}) as lexical senses. "
                       f"{len(senses)} synsets; synset id = stable sense id.",
        "content": {"senses": senses},
    }
    with open(out, "w") as f:
        json.dump(doc, f)
    print(f"wrote {len(senses)} senses to {out}")


if __name__ == "__main__":
    main()
