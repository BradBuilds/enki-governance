"""name_coref.py — document-scoped name coreference collapse.

Folds a bare single-token person name into its FULL identity when every
document they co-occur in agrees on the same fuller person (scope-safe:
resolve within the document, promote to a global merge only on agreement).
Ambiguous names (>1 distinct fuller across docs) are left untouched for
per-document splitting. Title/place false positives (X of Y, Duke/Queen/...)
are skipped. Merges go through the canonical entity_merge, so every fold is
recorded in entity_redirects (audit + reversal).
"""
from __future__ import annotations

import re
import logging

from storage.postgres.db import get_connection
from core.gdu.entity_merge import merge_entities

log = logging.getLogger("enki.gdu.name_coref")

# tokens that mark a title/place fuller, not a personal-name coref
_TITLE_RE = re.compile(
    r"\b(of|Queen|Duke|King|Prince|Princess|Lord|Lady|Sir|Dame|Dr|Judge|"
    r"General|Colonel|Senator|President|Governor|Mayor|Pope|Saint|St)\b", re.I)

_SQL = """
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
ent AS (
  SELECT pid, pname,
     count(*) FILTER (WHERE n_in_doc=1) AS docs_agree,
     count(DISTINCT fid) FILTER (WHERE n_in_doc=1) AS distinct_people,
     (array_agg(fid ORDER BY fid) FILTER (WHERE n_in_doc=1))[1] AS target_fid
  FROM per_doc GROUP BY 1,2)
SELECT e.pid, e.pname, e.target_fid, f.canonical_name, e.docs_agree
FROM ent e JOIN entities f ON f.entity_id=e.target_fid
WHERE e.distinct_people=1
ORDER BY e.docs_agree DESC
"""


def run(tenant_id: str = "default", batch: int = 15) -> dict:
    """Fold up to `batch` scope-safe bare names into their full identity.
    Highest-agreement first. Returns stats."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(_SQL)
            rows = cur.fetchall()

    stats = {"candidates": len(rows), "merged": 0, "flagged": 0, "skipped": 0}
    for pid, pname, fid, fuller, docs in rows:
        if _TITLE_RE.search(fuller or "") or (" of " in (" " + (fuller or "") + " ")):
            stats["flagged"] += 1
            continue
        if stats["merged"] >= batch:
            break
        try:
            res = merge_entities(fid, pid, reason="name_coref_docscoped_v1")
            if res.get("ok") is False:
                stats["skipped"] += 1
            else:
                stats["merged"] += 1
                log.info("coref: %s -> %s (%d docs agree)", pname, fuller, docs)
        except Exception as e:
            stats["skipped"] += 1
            log.warning("coref merge %s -> %s failed: %s", pname, fuller, e)
    return stats


if __name__ == "__main__":
    import sys
    b = int(sys.argv[1]) if len(sys.argv) > 1 else 15
    print(run(batch=b))
