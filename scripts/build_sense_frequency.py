#!/usr/bin/env python3
"""Build the per-(lemma, pos, synset) SENSE-FREQUENCY pack from Open English WordNet.

The dominant-sense prior. WordNet orders a word's senses by decreasing frequency
(sense 1 = most common) — `wn`'s `Word.senses()` returns them in that canonical
order. We capture, FOR EACH LEMMA, the rank of every synset it binds to. Rank is a
property of the (lemma, pos) pair, NOT the synset: a synset can be sense 1 for one
lemma and sense 5 for another, so this attaches to the form→concept BINDING, not
the concept. SemCor tag counts (`Sense.counts()`) are captured too but are 0 across
OEWN-2023 (the cntlist was dropped), so rank is the usable signal.

Output rows: {lemma, pos, synset_id, rank, n_senses, count}. The node-A loader matches
lemma → form_nodes.surface_form_normalized and synset_id → concept type_evidence
'sense_id', then writes freq = 1/rank into form_concept_bindings.binding_evidence.

Reproducible reference data (built from this converter + pinned OEWN), like the
wordnet pack itself — cached/scp'd, not committed.

  /tmp/wnbuild/bin/python scripts/build_sense_frequency.py [LEX] [OUT.json]
"""
import json
import re
import sys

import wn

POS = {"n": "noun", "v": "verb", "a": "adjective", "s": "adjective", "r": "adverb"}
_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    # match core.language.library_collapse._norm so lemmas align with stored forms
    return _WS.sub(" ", (s or "").lower()).strip()


def main():
    lex = sys.argv[1] if len(sys.argv) > 1 else "oewn:2023"
    out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/sense_frequency.json"

    w = wn.Wordnet(lex)
    rows = []
    n_words = 0
    for word in w.words():
        senses = word.senses()
        if not senses:
            continue
        lemma = _norm(word.lemma())
        pos = POS.get(word.pos, "other")
        n = len(senses)
        for rank, s in enumerate(senses, 1):
            cnt = s.counts()
            rows.append({
                "lemma": lemma,
                "pos": pos,
                "synset_id": s.synset().id,
                "rank": rank,
                "n_senses": n,
                "count": (cnt[0].value if cnt else 0),
            })
        n_words += 1

    doc = {
        "id": "sense.frequency",
        "type": "sense_frequency",
        "version": lex.replace(":", "-"),
        "description": f"Per-(lemma,pos,synset) WordNet sense rank (1=most frequent) "
                       f"from {lex}. {n_words} word entries, {len(rows)} sense ranks. "
                       f"Dominant-sense prior for the collapse equation.",
        "content": {"ranks": rows},
    }
    with open(out, "w") as f:
        json.dump(doc, f)
    print(f"wrote {len(rows)} sense ranks ({n_words} word entries) to {out}")


if __name__ == "__main__":
    main()
