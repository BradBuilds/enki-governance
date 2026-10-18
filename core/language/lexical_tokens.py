"""Entity-aware lexical tokenization — the COMMON-WORD content tokens of a text,
with entity / proper-noun spans removed so they are NOT resolved against WordNet.

The lexical sense layer (WordNet common senses) and the entity layer (people, orgs,
places) PARTITION a text. A proper noun is an entity — "Acme", "Palm Beach",
"United States", "Doe" — and must never be resolved as a WordNet common sense
("Acme"->a same-named painter, "palm"->the inner hand, "united"->an adjective).
Running the sense layer blind on entity-laden text produces exactly those
mis-mappings (validated on the Acme corpus). This router yields only the
common-word content for resolve_sense, using two signals:

  1. AUTHORITATIVE: mask document_mention spans (the extraction pipeline already
     tagged entities there) — char offsets are paragraph-relative.
  2. ROBUST BACKUP: capitalization. A token capitalized AWAY FROM a sentence start
     is being used as a proper noun → skip it (this catches entities the tagger
     missed, and is what distinguishes "Palm Beach" from "palm of the hand"). At a
     sentence start, capitalization is grammatical not nominal, so the token is
     kept (lowercased) — sentence-initial entities are caught by the mention mask.
     ALL-CAPS tokens (transcript speaker labels: "THE WITNESS", "MS.") are skipped.

Conservative by design: when a capitalized mid-sentence token might be a common
word, we abstain (skip) rather than risk an entity→common-sense mis-map.
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")
# A sentence/segment boundary: terminal punctuation + space, or a newline. After
# one of these, a capital is grammatical (sentence start), not a proper-noun signal.
_BOUNDARY = re.compile(r'[.!?:;]["\')\]]?\s+|\n+')


def _sentence_starts(text: str) -> set[int]:
    starts = {0}
    for m in _BOUNDARY.finditer(text):
        starts.add(m.end())
    return starts


def lexical_tokens(raw_text: str, mention_spans=(), stop=frozenset(),
                   min_len: int = 3) -> list[str]:
    """Lowercased common-word content tokens of `raw_text`, excluding entity spans,
    proper nouns, ALL-CAPS labels, stopwords, and tokens shorter than `min_len`.

    `mention_spans` is an iterable of (char_start, char_end) paragraph-relative
    entity spans (e.g. from document_mentions)."""
    if not raw_text:
        return []
    # 1) mask entity spans in place (preserve offsets so sentence-start detection
    #    on the ORIGINAL text stays valid)
    chars = list(raw_text)
    n = len(chars)
    for a, b in mention_spans:
        if a is None or b is None:
            continue
        for i in range(max(0, a), min(n, b)):
            chars[i] = " "
    masked = "".join(chars)
    starts = _sentence_starts(raw_text)

    out: list[str] = []
    for m in _WORD.finditer(masked):
        tok = m.group(0)
        low = tok.lower()
        if len(low) < min_len or low in stop:
            continue
        if tok.isupper() and len(tok) > 1:          # ALL-CAPS label → skip
            continue
        if tok[0].isupper() and m.start() not in starts:   # proper noun mid-sentence
            continue
        out.append(low)
    return out
