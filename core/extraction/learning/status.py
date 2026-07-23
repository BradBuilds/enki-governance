"""
CLI: show learned-mapping status.

Usage:
    .venv/bin/python -m core.extraction.learning.status
    .venv/bin/python -m core.extraction.learning.status --learn
    .venv/bin/python -m core.extraction.learning.status --show <signal_type>

Flags:
    --learn          Run the learner over all observing rows with enough
                     samples. Writes proposed_map + last_score back.
    --show <type>    Dump the current shape profile for one signal_type.
"""

from __future__ import annotations

import argparse
import json
import sys

import psycopg2

from core.extraction.learning.mapping_store import (
    get_status,
    learn_from_observations,
)
from core.extraction.learning.promoter import (
    promote,
    reject,
    list_candidates,
    auto_promote_eligible,
)
from core.extraction.learning.shape_profiler import get_profile
from storage.config import get_db_dsn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=None,
                    help="DSN (defaults to $ENKI_DB_DSN)")
    ap.add_argument("--learn", action="store_true",
                    help="run the learner over observing rows")
    ap.add_argument("--show", metavar="SIGNAL_TYPE",
                    help="dump the shape profile for a specific signal_type")
    ap.add_argument("--candidates", action="store_true",
                    help="list all candidate mappings awaiting review")
    ap.add_argument("--promote", metavar="SIGNAL_TYPE",
                    help="promote a candidate to a live learned YAML file")
    ap.add_argument("--reject", metavar="SIGNAL_TYPE",
                    help="reject a candidate and remove any promoted file")
    ap.add_argument("--auto-promote", action="store_true",
                    help="promote every candidate that meets the auto-policy thresholds")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --auto-promote, show eligible candidates without promoting")
    ap.add_argument("--reviewer", default="cli-user",
                    help="reviewer id recorded on promoted/rejected rows")
    args = ap.parse_args()

    dsn = args.dsn or get_db_dsn()
    try:
        conn = psycopg2.connect(dsn)
    except Exception as e:
        print(f"cannot connect to {dsn}: {e}", file=sys.stderr)
        return 2

    if args.show:
        prof = get_profile(args.show, conn)
        if prof is None:
            print(f"no profile for {args.show}")
            return 1
        print(json.dumps(prof, indent=2, default=str))
        return 0

    if args.candidates:
        cands = list_candidates(conn)
        if not cands:
            print("no candidates")
            return 0
        print(f"{'signal_type':45} {'samples':>8} {'score':>6} {'first_seen':>26}")
        print("-" * 90)
        for c in cands:
            score = f"{c['score']:.3f}" if c['score'] is not None else "  -  "
            print(f"{c['signal_type']:45} {c['samples']:>8} {score:>6} {c['first_seen'] or '-':>26}")
        return 0

    if args.promote:
        result = promote(conn, args.promote, reviewer=args.reviewer)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "promoted" else 1

    if args.reject:
        result = reject(conn, args.reject, reviewer=args.reviewer)
        print(json.dumps(result, indent=2, default=str))
        return 0 if result.get("status") == "rejected" else 1

    if args.auto_promote:
        results = auto_promote_eligible(conn, dry_run=args.dry_run)
        if not results:
            print("no candidates meet auto-promote thresholds")
            return 0
        action = "WOULD PROMOTE" if args.dry_run else "PROMOTED"
        for r in results:
            print(f"{action}: {r['signal_type']:45} "
                  f"samples={r['samples']} score={r['score']:.3f}")
            if not args.dry_run and "result" in r:
                print(f"    → {r['result'].get('path')}")
        return 0

    if args.learn:
        results = learn_from_observations(conn)
        if not results:
            print("no candidates — not enough samples, or table empty")
        else:
            print(f"{'signal_type':45} {'samples':>8} {'score':>6} {'status':>12} {'fields':>6}")
            print("-" * 82)
            for r in results:
                print(f"{r['signal_type']:45} {r['samples']:>8} "
                      f"{r['score']:>6.3f} {r['status']:>12} "
                      f"{r['mapping_fields']:>6}")
        print()

    rows = get_status(conn)
    if not rows:
        print("learned_mappings table is empty — no signals have hit no_worker_for_type yet")
        return 0

    print(f"{'signal_type':45} {'status':>12} {'samples':>8} {'score':>6} {'active_set':>11}")
    print("-" * 86)
    for r in rows:
        score = f"{r['score']:.3f}" if r['score'] is not None else "  -  "
        aset = r['admitted_under'] or "—"
        print(f"{r['signal_type']:45} {r['status']:>12} "
              f"{r['samples']:>8} {score:>6} {aset:>11}")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
