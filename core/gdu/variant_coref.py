"""variant_coref.py — fold NAME-VARIANT person entities into their canonical
full identity: titles (Ms./Defendant/Judge X), initials (G Doe),
no-space OCR (JaneDoe), possessives (Doe's).

Same governed shape as name_coref: match a variant to a clean full name by
SURNAME (+ given-hint) via document co-occurrence, resolve within a document,
promote to a global merge only when every document agrees. Different people
who share a surname (Jane vs Robert Doe) stay separate because their
documents disagree. Audited + reversible via entity_redirects.
"""
from __future__ import annotations
import re, logging
from storage.postgres.db import get_connection
from core.gdu.entity_merge import merge_entities

log = logging.getLogger("enki.gdu.variant_coref")

TITLE = (r"Ms|Mr|Mrs|Miss|Dr|Prof|Judge|Justice|Defendant|Sir|Lady|Lord|President|"
         r"Senator|Attorney|Agent|Officer|Detective|Rev|Gen|Col|Capt|Lt|Sgt|Amb|Gov|Rep|Hon|Mme|Madame|Monsieur")


def parse_variant(name: str):
    """Return {surname, given?/given_i?, kind} or None if not a variant form."""
    n = re.sub(r"[’'`]s$", "", name.strip()).strip()          # possessive
    n = re.sub(rf"^({TITLE})\.(?=\S)", r"\1. ", n)                  # Ms.Doe -> Ms. Doe
    m = re.match(r"^([A-Z][a-z]{2,})([A-Z][a-z]{2,})$", n)          # no-space camelCase
    if m:
        return {"surname": m.group(2).lower(), "given": m.group(1).lower(), "kind": "nospace"}
    stripped = re.sub(rf"^(?:{TITLE})\.?\s+", "", n).strip()
    had_title = stripped != n
    p = (stripped if had_title else n).split()
    if len(p) == 2 and re.match(r"^[A-Z]\.?$", p[0]) and re.match(r"^[A-Z][a-z-]+$", p[1]):
        return {"surname": p[1].lower(), "given_i": p[0][0].lower(),
                "kind": "title_initial" if had_title else "initial"}
    if had_title and len(p) == 1 and re.match(r"^[A-Z][a-z-]+$", p[0]):
        return {"surname": p[0].lower(), "given": None, "kind": "title"}
    return None


def _compat(fname: str, v: dict) -> bool:
    first = fname.split()[0].lower()
    if v.get("given_i"):
        return first.startswith(v["given_i"])
    if v.get("given"):
        return first == v["given"] or first.startswith(v["given"][:4])
    return True   # surname-only (title/bare) — any given accepted; scope decides


def _plan():
    """Return list of (variant_id, variant_name, target_id, target_name, docs_agree)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(rf"""
                SELECT e.entity_id, e.canonical_name FROM entities e
                 WHERE e.canonical_type='person'
                   AND length(e.canonical_name) BETWEEN 4 AND 30
                   AND (
                        e.canonical_name ~ '^(?:{TITLE})\.?\s'          -- title: Ms. / Defendant X
                     OR e.canonical_name ~ '^[A-Z]\.?\s[A-Z][a-z-]+$'   -- initial: G Doe
                     OR e.canonical_name ~ '^[A-Z][a-z]{{2,}}[A-Z][a-z]{{2,}}$'  -- no-space: JaneDoe
                     OR e.canonical_name ~ '[’''`]s$'                     -- possessive: Doe's
                   )
                   AND EXISTS (SELECT 1 FROM assessments a WHERE a.subj_entity_id=e.entity_id OR a.obj_entity_id=e.entity_id)
                   AND NOT EXISTS (SELECT 1 FROM entity_redirects r WHERE r.old_entity_id=e.entity_id)
            """)
            cands = cur.fetchall()

    plan = []
    for vid, vname in cands:
        v = parse_variant(vname)
        if not v:
            continue
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT dmp.document_id, f.entity_id, f.canonical_name
                    FROM document_mentions dmp
                    JOIN document_mentions dmf ON dmf.document_id=dmp.document_id AND dmf.entity_id<>dmp.entity_id
                    JOIN entities f ON f.entity_id=dmf.entity_id
                    WHERE dmp.entity_id=%s AND f.canonical_type='person'
                      AND f.canonical_name ~ '^[A-Z][A-Za-z.''-]+( [A-Z][A-Za-z.''-]+){1,2}$'
                      AND lower(regexp_replace(f.canonical_name, '^.* ', '')) = %s
                      AND f.entity_id <> %s
                    GROUP BY 1,2,3
                """, (vid, v["surname"], vid))
                rows = cur.fetchall()
        per_doc = {}
        fuller_docs = {}
        fuller_name = {}
        for doc, fid, fname in rows:
            if not _compat(fname, v):
                continue
            per_doc.setdefault(doc, set()).add(fid)
            fuller_docs.setdefault(fid, set()).add(doc)
            fuller_name[fid] = fname
        if not per_doc:
            continue
        total_docs = len(per_doc)
        # strict: documents where exactly one compatible full name appears
        resolved = [next(iter(s)) for s in per_doc.values() if len(s) == 1]
        strict = set(resolved)
        target, agree = None, 0
        if len(strict) == 1:
            target = next(iter(strict)); agree = len(resolved)
        elif not v.get("given") and not v.get("given_i"):
            # title/possessive surname-only: per-doc can't isolate when the
            # whole family co-occurs (legal docs). Fall back to CORPUS
            # dominance — the same-surname full person the variant co-occurs
            # with in the most documents, if it clearly dominates.
            ranked = sorted(fuller_docs.items(), key=lambda kv: -len(kv[1]))
            top, topn = ranked[0][0], len(ranked[0][1])
            second = len(ranked[1][1]) if len(ranked) > 1 else 0
            if topn >= 0.5 * total_docs and topn >= 2 * max(1, second):
                target, agree = top, topn
        if target and target != vid:
            plan.append((vid, vname, target, fuller_name[target], agree))
    return plan


def run(dry_run: bool = True) -> dict:
    plan = _plan()
    stats = {"candidates": len(plan), "merged": 0, "skipped": 0}
    if dry_run:
        stats["plan"] = [(p[1], p[3], p[4]) for p in plan]
        return stats
    for vid, vname, tid, tname, docs in plan:
        try:
            res = merge_entities(tid, vid, reason="variant_coref_v1")
            stats["merged" if res.get("ok") is not False else "skipped"] += 1
        except Exception as e:
            stats["skipped"] += 1
            log.warning("variant merge %s -> %s: %s", vname, tname, e)
    return stats


if __name__ == "__main__":
    import sys, json
    r = run(dry_run="--apply" not in sys.argv)
    if "plan" in r:
        for pn, tn, d in r["plan"][:60]:
            print(f"  {pn:>24}  ->  {tn:<26} ({d} docs)")
        print("TOTAL candidates:", r["candidates"])
    else:
        print(r)
