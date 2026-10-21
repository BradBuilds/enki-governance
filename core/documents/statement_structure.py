"""Record the sentence structure; project the connection from it.

`scripts/statement_promoter.py` has produced this structure since it was
written and was deliberately left disconnected — the "map without the GDU"
prototype. This connects it to the corpus, with two changes the measurements
demanded.

**The predicate resolves against VERBS only.** Unconstrained, 45% of the
resolver's confident answers land on a noun or adjective — `at` collapses to a
Laotian currency unit, `v` to the 22nd letter of the alphabet, `held` to an
adjective. Constraining raises the resolution rate as well, 53% -> 75%,
because the right verb sense was being crowded out by an adjective that
matched the surface string more exactly.

**The structure is the record; the triple is a view.** Storing only
`addr(A) -> addr(V) -> addr(B)` throws away what produced it, and then you
cannot re-resolve when the substrate changes, cannot tell a denial from an
assertion, and cannot see that an address is doing two jobs. In the
expansion/contraction frame this table IS the expansion: an address that
cannot be expanded back to its evidence cannot refute a hallucination.

Nothing here writes a collapse decision (`write_decision=False`) and nothing
here promotes. Records carry no authority — frequency is evidence, not
permission.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Optional

log = logging.getLogger("enki.documents.statement_structure")

RESOLVER_VERSION = "v1-verbconstrained"

# A structure may federate only if its predicate anchored to a real sense.
# Parked predicates are node-local: another node's park may land elsewhere.
FEDERATABLE_ACTIONS = ("collapse",)


def connection_key(subject_type: str, predicate_sense_address: str,
                   object_type: str, polarity: str, modality: str) -> str:
    """Content-derived identity for a connection.

    Never lemma strings and never edge-type names — two nodes that share no
    documents put the same connection at the same key and merge by sum, and a
    connection can be minted, counted and federated before anyone decides what
    it is called.

    Polarity and modality are INSIDE the key. A denial is a different
    connection, not a negative count of the same one; collapsing them would
    destroy the contrast the comparison exists to find.
    """
    src = ":".join([subject_type or "", predicate_sense_address or "",
                    object_type or "", polarity or "", modality or ""])
    return hashlib.sha256(src.encode()).hexdigest()[:16]


def resolve_predicate_sense(get_conn, verb: str, *, domain: Optional[str] = None,
                            tenant: str = "default") -> dict:
    """Resolve a predicate to a verb sense through the governed equation.

    Candidates are constrained to `lexical.verb`. Writes no collapse decision —
    this is a reading, not a ruling.
    """
    out = {"address": None, "action": "park", "gloss": None}
    if not verb:
        return out
    try:
        from core.language.sense_resolver import resolve_sense
        r = resolve_sense(verb, get_conn, domain=domain, tenant=tenant,
                          lemmatize=True, write_decision=False, pos="verb")
        out["address"] = getattr(r, "address", None)
        out["action"] = getattr(r, "action", "park")
    except TypeError:
        # An older sense_resolver without the pos constraint. Resolving
        # unconstrained here would manufacture noun-sense predicates at scale,
        # which is the exact defect this module exists to avoid — so park.
        log.warning("sense_resolver has no pos constraint; parking predicate %r", verb)
        out["action"] = "park"
    except Exception:
        log.exception("predicate resolution failed for %r", verb)
        out["action"] = "park"
    return out


def _entity_type(cur, entity_id: Optional[int]) -> Optional[str]:
    if not entity_id:
        return None
    cur.execute("SELECT canonical_type FROM entities WHERE entity_id = %s",
                (entity_id,))
    row = cur.fetchone()
    return row[0] if row else None


def build_structure(cur, get_conn, row: dict, *,
                    tenant: str = "default") -> dict:
    """Build (do not persist) the structure for one statement row."""
    verb = (row.get("predicate_lemma") or row.get("predicate_verb_surface") or "")
    pred = resolve_predicate_sense(get_conn, verb, tenant=tenant)

    agent_type = _entity_type(cur, row.get("speaker_entity_id"))
    object_type = _entity_type(cur, row.get("object_entity_id"))
    # An event-shaped statement points at a time or a place rather than an
    # entity. That is a referent, not an absence — 1,881 statements in the
    # corpus are this shape and every one was rejected as a type mismatch
    # against a null destination type.
    if not object_type:
        if row.get("event_location_entity_id"):
            object_type = _entity_type(cur, row["event_location_entity_id"]) or "place"
        elif row.get("event_time"):
            object_type = "event"

    polarity = row.get("polarity") or "affirmative"
    modality = row.get("modality") or "asserted"
    federatable = pred["action"] in FEDERATABLE_ACTIONS

    return {
        "statement_id": row["statement_id"],
        "tenant_id": tenant,
        "agent_entity_id": row.get("speaker_entity_id"),
        "agent_type": agent_type,
        "predicate_surface": row.get("predicate_verb_surface"),
        "predicate_lemma": row.get("predicate_lemma"),
        "predicate_sense_address": pred["address"],
        "predicate_action": pred["action"],
        "predicate_gloss": pred.get("gloss"),
        "polarity": polarity,
        "modality": modality,
        "object_entity_id": row.get("object_entity_id"),
        "object_type": object_type,
        "object_text": row.get("object_text"),
        "roles": {},
        "terms": [],
        "terms_resolved": 0,
        "terms_total": 0,
        "proposition": row.get("raw_text"),
        "document_id": row.get("document_id"),
        "library_id": row.get("library_id"),
        "source_time": None,
        "event_time": row.get("event_time"),
        "federatable": federatable,
        "resolver_version": RESOLVER_VERSION,
    }


def project_connection(structure: dict) -> Optional[dict]:
    """The view over the structure. Returns None when there is nothing to
    project — a parked predicate or a missing endpoint type is not a failure,
    it is a structure that has not earned a projection yet."""
    if structure.get("predicate_action") != "collapse":
        return None
    addr = structure.get("predicate_sense_address")
    subj, obj = structure.get("agent_type"), structure.get("object_type")
    if not (addr and subj and obj):
        return None
    return {
        "connection_key": connection_key(subj, addr, obj,
                                         structure["polarity"], structure["modality"]),
        "subject_type": subj,
        "predicate_sense_address": addr,
        "object_type": obj,
        "polarity": structure["polarity"],
        "modality": structure["modality"],
        "statement_id": structure["statement_id"],
        "document_id": structure.get("document_id"),
        "library_id": structure.get("library_id"),
        "federatable": structure.get("federatable", False),
        "tenant_id": structure.get("tenant_id", "default"),
    }


_STRUCT_COLS = (
    "statement_id", "tenant_id", "agent_entity_id", "agent_type",
    "predicate_surface", "predicate_lemma", "predicate_sense_address",
    "predicate_action", "predicate_gloss", "polarity", "modality",
    "object_entity_id", "object_type", "object_text", "roles", "terms",
    "terms_resolved", "terms_total", "proposition", "document_id",
    "library_id", "source_time", "event_time", "federatable",
    "resolver_version",
)


def persist_structure(cur, structure: dict) -> int:
    """Upsert the structure. Re-running must not create a second row for the
    same statement — corroboration is a count of distinct documents, so a
    duplicated structure would inflate it."""
    vals = []
    for c in _STRUCT_COLS:
        v = structure.get(c)
        vals.append(json.dumps(v) if c in ("roles", "terms") else v)
    placeholders = ", ".join(["%s"] * len(_STRUCT_COLS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _STRUCT_COLS
                        if c != "statement_id")
    cur.execute(
        f"""
        INSERT INTO statement_structures ({', '.join(_STRUCT_COLS)})
        VALUES ({placeholders})
        ON CONFLICT (statement_id) DO UPDATE SET {updates}
        RETURNING structure_id
        """,
        vals,
    )
    return cur.fetchone()[0]


def persist_connection(cur, structure_id: int, conn_row: dict) -> None:
    """Record one observation of a connection. Never written without its
    structure — the projection is disposable, the structure is not."""
    cur.execute(
        """
        INSERT INTO connection_observations
            (connection_key, tenant_id, subject_type, predicate_sense_address,
             object_type, polarity, modality, structure_id, statement_id,
             document_id, library_id, federatable)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (connection_key, statement_id) DO NOTHING
        """,
        (conn_row["connection_key"], conn_row.get("tenant_id", "default"),
         conn_row["subject_type"], conn_row["predicate_sense_address"],
         conn_row["object_type"], conn_row["polarity"], conn_row["modality"],
         structure_id, conn_row["statement_id"], conn_row.get("document_id"),
         conn_row.get("library_id"), conn_row.get("federatable", False)),
    )


def record(cur, get_conn, row: dict, *, tenant: str = "default") -> dict:
    """Build, persist, project, persist. Returns what happened."""
    structure = build_structure(cur, get_conn, row, tenant=tenant)
    structure_id = persist_structure(cur, structure)
    conn_row = project_connection(structure)
    if conn_row:
        persist_connection(cur, structure_id, conn_row)
    return {"structure_id": structure_id,
            "predicate_action": structure["predicate_action"],
            "projected": bool(conn_row),
            "connection_key": conn_row["connection_key"] if conn_row else None}
