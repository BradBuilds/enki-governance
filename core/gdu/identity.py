"""Cross-node entity identity key — the single source of truth.

`canonical_entity_key` is the primitive that lets two nodes on different
computers recognize the same real-world entity. Two entities with the same
key ARE the same entity for federation/merge purposes.

THIS MODULE IS THE SPEC. Everything that computes the key delegates here or
mirrors it:
  1. This Python function — used by publisher + subscriber (G8 verify) + cards_api.
  2. The plpgsql trigger `compute_canonical_entity_key()` (migration 124),
     which stamps the column on EVERY insert/update so no write path can skip
     it. The trigger mirrors this logic byte-for-byte.
  3. (legacy) the one-time SQL backfill in migration 104 — superseded by the
     trigger; only relevant to pre-124 rows.

`data_integrity.check_ckey_python_sql_agree()` asserts (1) and (2) agree at
runtime so they can never silently drift.

KEY DERIVATION (current — Phase 1A):
  sha256("<whitespace-normalized lower canonical_name>|<lower canonical_type>")

This is byte-identical to the prior `entity_publisher.canonical_key`, so the
federation envelope and the subscriber's G8 verification are unchanged. The
Phase 1A win is purely robustness: ONE implementation, and a trigger that
guarantees the column is populated on every insert path (previously new rows
from the gateway/promotion/fission/subscriber paths got a NULL key and never
federated).

DEFERRED to Phase 1B (needs the federation envelope to carry identifiers, plus
a guarded secondary merge):
  * identifier-first keying — strongest identifier (QID/CIK/…) wins, so two
    nodes that resolved the same entity to the same QID converge regardless of
    surface form. IDENTIFIER_PRECEDENCE + strongest_identifier() below are the
    building blocks; the merge worker (entity_merge) will use them to reconcile
    variant surfaces / partial-identifier sets with an identifier-conflict
    guard. Kept out of the primary key for now so G8 and the envelope schema
    stay stable.
"""
from __future__ import annotations

import hashlib
import re

# Strongest-first identifier precedence. NOT yet used by the primary key
# (Phase 1B) — consumed by the secondary merge pass to converge entities that
# share a strong identifier even when their names/keys differ.
IDENTIFIER_PRECEDENCE: tuple[str, ...] = (
    "qid", "wikidata", "wikidata_id",   # Wikidata QID — globally unique, strongest
                                        # (wikidata_id is the enricher's key spelling)
    "lei",                       # Legal Entity Identifier
    "cik",                       # SEC Central Index Key
    "duns",                      # Dun & Bradstreet
    "isin",                      # securities
    "permid",                    # Refinitiv
    "imo", "mmsi",               # vessels
    "icao24", "tail_number", "registration",   # aircraft
    "cage", "ncage",             # defense contractors
    "fec_id", "bioguide_id",     # US politics
    "ror", "grid", "isni", "orcid",  # research orgs / people
    "ein",                       # US tax id
)

_WS = re.compile(r"\s+")


def _sha256_hex(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def strongest_identifier(identifiers: dict | None) -> tuple[str, str] | None:
    """Return (emitted_type, value) of the highest-precedence identifier
    present, or None. Used by the Phase 1B merge pass (not the primary key)."""
    if not identifiers:
        return None
    norm: dict[str, str] = {}
    for k, v in identifiers.items():
        if v is None:
            continue
        key = str(k).strip().lower()
        val = str(v).strip()
        if val and val.lower() not in ("null", "none", "n/a"):
            norm.setdefault(key, val)
    for key in IDENTIFIER_PRECEDENCE:
        if key in norm:
            emitted = "qid" if key in ("wikidata", "wikidata_id") else key
            return (emitted, norm[key])
    return None


def _norm_identifiers(identifiers: dict | None) -> dict[str, str]:
    """Lowercased {key: value} with blanks/sentinels dropped."""
    out: dict[str, str] = {}
    if not identifiers:
        return out
    for k, v in identifiers.items():
        if v is None:
            continue
        key = str(k).strip().lower()
        val = str(v).strip()
        if val and val.lower() not in ("null", "none", "n/a"):
            out.setdefault(key, val)
    return out


def identifier_conflict(a: dict | None, b: dict | None) -> bool:
    """True if a and b share an identifier KEY with DIFFERENT values — strong
    evidence they are different real-world entities (e.g. two different QIDs).
    Such pairs must NEVER be auto-merged."""
    na, nb = _norm_identifiers(a), _norm_identifiers(b)
    for key in na.keys() & nb.keys():
        if na[key].lower() != nb[key].lower():
            return True
    return False


def shared_strong_identifier(a: dict | None, b: dict | None) -> tuple[str, str] | None:
    """Return (type, value) of a STRONG identifier (in IDENTIFIER_PRECEDENCE)
    present in BOTH with the same value — strong evidence they are the same
    real-world entity. None if no such shared identifier."""
    na, nb = _norm_identifiers(a), _norm_identifiers(b)
    for key in IDENTIFIER_PRECEDENCE:
        if key in na and key in nb and na[key].lower() == nb[key].lower():
            emitted = "qid" if key in ("wikidata", "wikidata_id") else key
            return (emitted, na[key])
    return None


def canonical_entity_key(
    canonical_name: str | None,
    canonical_type: str | None,
    identifiers: dict | None = None,
    normalized_name: str | None = None,   # reserved
) -> str:
    """The cross-node identity key: sha256(name | type | strongest_id).

    The strongest identifier (by IDENTIFIER_PRECEDENCE) is folded into the key
    so it DISAMBIGUATES same-named-but-distinct entities: two companies both
    named "Acme" with different CIKs get different keys and correctly stay
    separate, while the name still keeps differently-named entities apart even
    when a (possibly bad) identifier is shared. Entities with no identifier fall
    back to a pure name|type key. The merge that converges variant surfaces of
    the SAME identified entity (e.g. "Boeing Company" ↔ "The Boeing Company",
    shared CIK) happens in the resolver BEFORE the key is computed, so the
    survivor carries one canonical name + one id → one key.

    Mirrors the plpgsql compute_canonical_entity_key() (migration 124)
    byte-for-byte; data_integrity.check_ckey_python_sql_agree() asserts it.
    """
    etype = (canonical_type or "").lower().strip()
    name = _WS.sub(" ", (canonical_name or "").lower()).strip()
    ident = strongest_identifier(identifiers)
    id_tok = f"{ident[0]}:{ident[1].lower()}" if ident else ""
    return _sha256_hex(f"{name}|{etype}|{id_tok}")
