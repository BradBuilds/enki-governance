"""name_split.py — document-scoped split of CONFLATED bare-name entities.

A bare name (e.g. 'Acme') that resolves to DIFFERENT people in different
documents is a conflated node. We dissolve it by re-assigning its mentions
PER DOCUMENT to the doc-local full identity — but only for documents that
have exactly ONE clean fuller (2-3 capitalized words; fragments/titles
skipped). Docs with 0 or >1 in-doc fullers are left untouched.

statements carry document_id so they are reassigned per-doc directly.
assessments do NOT (they aggregate source_statement_ids across docs), so the
conflated entity's assessments are dropped and its statements reset to
pending; the governed promote pass re-derives correct per-person assessments
from the corrected substrate.

Every mention/statement move is logged to reassignment_audit (reversible).
"""
from __future__ import annotations
import logging
from storage.postgres.db import get_connection
from core.documents.assessment_promoter import promote_pending

log = logging.getLogger("enki.gdu.name_split")

_PLAN_SQL = """
WITH partial AS (
  SELECT e.entity_id, e.canonical_name, lower(e.canonical_name) AS tok
  FROM entities e
  WHERE e.canonical_type='person'
    AND e.canonical_name ~ '^[A-Z][A-Za-z''-]+$' AND length(e.canonical_name) BETWEEN 3 AND 20
    AND EXISTS (SELECT 1 FROM assessments a WHERE a.subj_entity_id=e.entity_id OR a.obj_entity_id=e.entity_id)
    AND NOT EXISTS (SELECT 1 FROM entity_redirects r WHERE r.old_entity_id=e.entity_id)),
per_doc AS (
  SELECT p.entity_id AS pid, p.canonical_name AS pname, dmp.document_id AS doc,
         count(DISTINCT f.entity_id) AS n_in_doc, min(f.entity_id) AS fid
  FROM partial p
  JOIN document_mentions dmp ON dmp.entity_id = p.entity_id
  JOIN document_mentions dmf ON dmf.document_id = dmp.document_id AND dmf.entity_id <> p.entity_id
  JOIN entities f ON f.entity_id = dmf.entity_id
  WHERE f.canonical_type='person' AND f.canonical_name LIKE '% %'
    AND (' '||lower(f.canonical_name)||' ') LIKE ('% '||p.tok||' %')
  GROUP BY 1,2,3),
conflated AS (
  SELECT pid FROM per_doc WHERE n_in_doc=1 GROUP BY pid HAVING count(DISTINCT fid)>1)
SELECT pd.pid, pd.pname, pd.doc, pd.fid, f.canonical_name
FROM per_doc pd
JOIN conflated c ON c.pid = pd.pid
JOIN entities f ON f.entity_id = pd.fid
WHERE pd.n_in_doc = 1
  AND f.canonical_name ~ '^[A-Z][A-Za-z.''-]+( [A-Z][A-Za-z.''-]+){1,2}$'
ORDER BY pd.pid, pd.doc
"""


def run(tenant_id: str = "default", dry_run: bool = True, max_entities: int | None = None) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_PLAN_SQL)
            plan = cur.fetchall()  # (pid, pname, doc, fid, fname)

    ents = sorted({r[0] for r in plan})
    if max_entities is not None:
        keep = set(ents[:max_entities])
        plan = [r for r in plan if r[0] in keep]
        ents = [e for e in ents if e in keep]

    stats = {"conflated_entities": len(ents), "reassignments": len(plan),
             "target_people": len({r[3] for r in plan}),
             "mentions_moved": 0, "assessments_rebuilt": 0}
    if dry_run or not plan:
        stats["dry_run"] = True
        return stats

    conf_ids = ents
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS reassignment_audit(
                id bigserial PRIMARY KEY, old_entity_id bigint, new_entity_id bigint,
                document_id bigint, mention_ids bigint[], statement_ids bigint[],
                reason text, applied_at timestamptz DEFAULT now())""")

            # statements that reference any conflated entity — captured BEFORE
            # reassignment so we can reset exactly these to pending afterwards.
            cur.execute("""SELECT array_agg(statement_id) FROM statements
                            WHERE speaker_entity_id = ANY(%s) OR object_entity_id = ANY(%s)""",
                        (conf_ids, conf_ids))
            reset_ids = cur.fetchone()[0] or []

            for pid, pname, doc, fid, fname in plan:
                cur.execute("SELECT array_agg(mention_id) FROM document_mentions WHERE entity_id=%s AND document_id=%s", (pid, doc))
                mids = cur.fetchone()[0] or []
                cur.execute("""SELECT array_agg(statement_id) FROM statements
                                WHERE document_id=%s AND (speaker_entity_id=%s OR object_entity_id=%s)""", (doc, pid, pid))
                sids = cur.fetchone()[0] or []
                cur.execute("UPDATE document_mentions SET entity_id=%s WHERE entity_id=%s AND document_id=%s", (fid, pid, doc))
                stats["mentions_moved"] += cur.rowcount or 0
                cur.execute("UPDATE statements SET speaker_entity_id=%s WHERE speaker_entity_id=%s AND document_id=%s", (fid, pid, doc))
                cur.execute("UPDATE statements SET object_entity_id=%s WHERE object_entity_id=%s AND document_id=%s", (fid, pid, doc))
                cur.execute("""INSERT INTO reassignment_audit(old_entity_id,new_entity_id,document_id,mention_ids,statement_ids,reason)
                               VALUES (%s,%s,%s,%s,%s,'name_split_docscoped_v1')""", (pid, fid, doc, mids, sids))

            # rebuild the conflated neighbourhood from the corrected substrate
            cur.execute("""UPDATE statements SET validation_status='pending', validation_reason=NULL
                            WHERE statement_id = ANY(%s)""", (reset_ids,))
            cur.execute("""DELETE FROM library_members WHERE member_kind='assessment'
                            AND member_id IN (SELECT assessment_id FROM assessments
                                              WHERE subj_entity_id=ANY(%s) OR obj_entity_id=ANY(%s))""", (conf_ids, conf_ids))
            cur.execute("DELETE FROM assessments WHERE subj_entity_id=ANY(%s) OR obj_entity_id=ANY(%s)", (conf_ids, conf_ids))
            stats["stale_assessments_deleted"] = cur.rowcount or 0
        conn.commit()

    pp = promote_pending(tenant_id)
    stats["assessments_rebuilt"] = pp.get("new_assessments", 0)
    return stats


if __name__ == "__main__":
    import sys
    dry = "--apply" not in sys.argv
    me = None
    for a in sys.argv:
        if a.startswith("--max="):
            me = int(a.split("=")[1])
    print(run(dry_run=dry, max_entities=me))
