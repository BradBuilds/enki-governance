"""Scoped → global alias promotion (understand the global fix).

Project/library-scoped aliases (AA→Acme in the flight-log corpus) are LOCAL
fixes. When the SAME surface→entity resolution is independently recorded across
enough distinct scopes, that local fix has become globally true — promote it to a
`scope='global'` alias. Mirrors the tier-E corroboration rule
(core/ldu/promote.py: ≥2 independent sources → promote).

The aggregation is also the report that lets an operator UNDERSTAND the global
picture: what's promotable, what stays ambiguous everywhere (needs a rule, not a
guess), and what garbage keeps getting dropped (needs an extraction-gate fix).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger("enki.workers.alias_global_promoter")

MIN_SCOPES = 2          # independent scopes that must agree (tier-E style)
CHECK_INTERVAL = 900    # 15 min

_WORKER_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()


def promote_global(min_scopes: int = MIN_SCOPES, tenant_id: str = "default",
                   dsn: Optional[str] = None) -> list[dict]:
    """Find surface→entity aliases corroborated across ≥min_scopes DISTINCT non-global
    scopes and write a global alias for each. Returns the promotions made."""
    promoted = []
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT normalized, entity_id,
                       COUNT(DISTINCT (scope_kind, scope_id)) AS scopes,
                       MAX(surface_form) AS surface, AVG(confidence) AS conf
                  FROM entity_aliases
                 WHERE tenant_id = %s AND scope_kind <> 'global'
                   AND validation_status IN ('validated_by_collapse','operator')
                 GROUP BY normalized, entity_id
                HAVING COUNT(DISTINCT (scope_kind, scope_id)) >= %s
            """, (tenant_id, min_scopes))
            rows = cur.fetchall()
            for norm, entity_id, scopes, surface, conf in rows:
                # NEVER promote a bare initial / short token to global — those are
                # inherently context-dependent ("AA" ≠ Acme outside the Acme
                # corpus), which is exactly why they were scoped. Only substantive
                # multi-token / full-name aliases become global.
                if " " not in norm and len(norm) <= 4:
                    continue
                # skip if already global
                cur.execute("""SELECT 1 FROM entity_aliases WHERE tenant_id=%s
                               AND normalized=%s AND entity_id=%s AND scope_kind='global'""",
                            (tenant_id, norm, entity_id))
                if cur.fetchone():
                    continue
                cur.execute("""
                    INSERT INTO entity_aliases
                        (tenant_id, entity_id, surface_form, normalized, scope_kind, scope_id,
                         confidence, evidence, validation_status, source)
                    VALUES (%s,%s,%s,%s,'global',NULL,%s,%s,'validated_by_collapse','global_promoter')
                    ON CONFLICT DO NOTHING
                """, (tenant_id, entity_id, surface, norm, float(conf or 0.0),
                      json.dumps({"promoted_from_scopes": int(scopes)})))
                if cur.rowcount:
                    promoted.append({"surface": surface, "entity_id": entity_id,
                                     "corroborating_scopes": int(scopes)})
        conn.commit()
    if promoted:
        log.info("promoted %d aliases scoped→global", len(promoted))
    return promoted


def global_report(tenant_id: str = "default", dsn: Optional[str] = None) -> dict:
    """The 'understand the global fix' digest: promotable (scopes agree),
    ambiguous-everywhere (parks across scopes), recurring-drop (garbage patterns)."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT MAX(surface_form), entity_id,
                       COUNT(DISTINCT (scope_kind, scope_id)) AS scopes
                  FROM entity_aliases
                 WHERE tenant_id=%s AND scope_kind<>'global'
                 GROUP BY normalized, entity_id
                 ORDER BY scopes DESC LIMIT 40
            """, (tenant_id,))
            promotable = [{"surface": r[0], "entity_id": r[1], "scopes": r[2]}
                          for r in cur.fetchall() if r[2] >= MIN_SCOPES]

            cur.execute("""
                SELECT raw_name, COUNT(DISTINCT (evidence::jsonb->>'scope_id')) AS scopes
                  FROM entity_ldu_p
                 WHERE tenant_id=%s AND source_pipeline='alias_collapse' AND status='pending'
                   AND evidence IS NOT NULL
                 GROUP BY raw_name
                HAVING COUNT(DISTINCT (evidence::jsonb->>'scope_id')) >= %s
                 ORDER BY scopes DESC LIMIT 20
            """, (tenant_id, MIN_SCOPES))
            ambiguous_everywhere = [{"surface": r[0], "scopes": r[1]} for r in cur.fetchall()]

            cur.execute("""
                SELECT raw_name, COUNT(*) FROM entity_ldu_p
                 WHERE tenant_id=%s AND status='rejected'
                 GROUP BY raw_name HAVING COUNT(*) >= 2 ORDER BY 2 DESC LIMIT 20
            """, (tenant_id,))
            recurring_drop = [{"surface": r[0], "times": r[1]} for r in cur.fetchall()]
    return {"promotable": promotable, "ambiguous_everywhere": ambiguous_everywhere,
            "recurring_drop": recurring_drop}


def _loop(tenant_id: str):
    while not _STOP.is_set():
        try:
            promote_global(tenant_id=tenant_id)
        except Exception as e:
            log.warning("global promoter tick failed: %s", e)
        for _ in range(CHECK_INTERVAL):
            if _STOP.is_set():
                break
            time.sleep(1)


def start_worker(tenant_id: str = "default"):
    global _WORKER_THREAD
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return {"status": "already_running"}
    _STOP.clear()
    _WORKER_THREAD = threading.Thread(target=_loop, args=(tenant_id,),
                                      name="alias-global-promoter", daemon=True)
    _WORKER_THREAD.start()
    return {"status": "started"}


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()
    if args.report:
        print(json.dumps(global_report(), indent=2, default=str))
    else:
        print(json.dumps(promote_global(), indent=2, default=str))
