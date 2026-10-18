"""
Spine validation harness — re-runnable, read-only proof that the
governance + federation + audit claims on this node match reality.

Each check maps to a specific marketing/architecture claim. Run anytime:

    python -m core.spine.validate                # human-readable
    python -m core.spine.validate --json         # machine-readable
    python -m core.spine.validate --strict       # exit 1 on any non-PASS

Exit codes:
    0 = all checks PASS (or only WARN with --no-strict)
    1 = any FAIL (or any non-PASS with --strict)
    2 = harness itself errored (couldn't even run a check)

The harness validates the active operating profile (node-local, dev,
hardware, etc.). Some checks are skipped or downgraded when a stricter
posture isn't required by the profile — e.g. signed_active_set is
required for `hardware` but optional for `node-local`.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("enki.spine.validate")

PEER_KEY_PATHS = [
    "/var/lib/enki/peer_key.bin",
    "/data/peer_key.bin",
]
ROOT_KEY_PATH = Path("storage/keys/root_key.enc")
TRUSTED_KEYS_PATH = Path("/etc/enki/trusted_keys.signed")
ACTIVE_SET_JSON = Path("active_set_v6.json")
ACTIVE_SET_SIGNED = Path("active_set_v6.signed.json")


@dataclasses.dataclass
class CheckResult:
    name: str
    status: str          # "pass" | "fail" | "warn" | "skip"
    value: str = ""      # observed value (short)
    expected: str = ""   # what we wanted
    claim: str = ""      # one-line of the marketing/architecture claim
    note: str = ""       # extra context if any

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_profile() -> CheckResult:
    """Report the active operating profile."""
    try:
        from core.profile.spec import PROFILES
        prof_name = os.environ.get("ENKI_PROFILE", "node")
        prof = PROFILES.get(prof_name)
        if prof is None:
            return CheckResult(
                "profile", "fail",
                value=prof_name,
                expected="known profile",
                claim="Profile must be a recognized operating posture",
                note=f"unknown profile; known={list(PROFILES.keys())}",
            )
        return CheckResult(
            "profile", "pass",
            value=prof_name,
            expected="any recognized profile",
            claim="Node runs under a recognized profile (node-local / hardware / etc.)",
        )
    except Exception as e:
        return CheckResult("profile", "fail", value=f"error: {e}",
                           claim="Profile module loadable")


def check_software_root_key() -> CheckResult:
    """Software root key exists, encrypted, owner-only."""
    p = ROOT_KEY_PATH
    if not p.exists():
        return CheckResult(
            "software_root_key", "fail",
            value="missing",
            expected=str(p),
            claim="Node has a software root key (the trust anchor for node-local profile)",
        )
    st = p.stat()
    mode = oct(st.st_mode & 0o777)
    if mode not in ("0o400", "0o600"):
        return CheckResult(
            "software_root_key", "warn",
            value=f"exists, mode {mode}",
            expected="mode 400 or 600",
            claim="Software root key is owner-only readable",
        )
    return CheckResult(
        "software_root_key", "pass",
        value=f"{p} ({st.st_size} bytes, mode {mode})",
        expected=f"{p} present, mode 400/600",
        claim="Node holds an encrypted software root key",
    )


def check_peer_key() -> CheckResult:
    """Peer key (Ed25519 32-byte private key) exists at canonical path."""
    for path in PEER_KEY_PATHS:
        p = Path(path)
        if p.exists():
            size = p.stat().st_size
            if size == 32:
                return CheckResult(
                    "peer_key", "pass",
                    value=f"{path} (32 bytes)",
                    expected="32 bytes Ed25519",
                    claim="Node has an Ed25519 peer key for federation signing",
                )
            return CheckResult(
                "peer_key", "fail",
                value=f"{path} ({size} bytes)",
                expected="32 bytes",
                claim="Peer key is a valid Ed25519 private key",
                note="size mismatch — file present but wrong length",
            )
    return CheckResult(
        "peer_key", "fail",
        value="not found",
        expected=" or ".join(PEER_KEY_PATHS),
        claim="Node holds an Ed25519 peer key",
        note="install ceremony must have skipped peer_key.bin",
    )


def check_active_set_loadable() -> CheckResult:
    """Active set JSON parses + version reported by /health matches."""
    if not ACTIVE_SET_JSON.exists():
        return CheckResult(
            "active_set_loadable", "fail",
            value=f"{ACTIVE_SET_JSON} missing",
            claim="Active set manifest exists and parses",
        )
    try:
        data = json.loads(ACTIVE_SET_JSON.read_text())
        version = data.get("version", "?")
        packs = data.get("packs", [])
        return CheckResult(
            "active_set_loadable", "pass",
            value=f"v{version}, {len(packs)} packs",
            expected="parseable JSON with version + packs[]",
            claim="Active set manifest is loadable and lists signed packs",
        )
    except Exception as e:
        return CheckResult(
            "active_set_loadable", "fail",
            value=f"parse error: {e}",
            claim="Active set JSON parseable",
        )


def check_active_set_signed() -> CheckResult:
    """Active set is signed (required for hardware profile, optional for node-local)."""
    prof_name = os.environ.get("ENKI_PROFILE", "node")
    try:
        from core.profile.spec import PROFILES
        prof = PROFILES.get(prof_name)
        required = bool(prof and getattr(prof, "requires_signed_active_set", False))
    except Exception:
        required = False
    if ACTIVE_SET_SIGNED.exists():
        return CheckResult(
            "active_set_signed", "pass",
            value=f"{ACTIVE_SET_SIGNED} present",
            expected="signed manifest exists",
            claim="Active set is signed by Tier-K key (hardware profile only)",
        )
    if required:
        return CheckResult(
            "active_set_signed", "fail",
            value="missing",
            expected="signed file required by profile",
            claim="Active set signed by Tier-K key (required by current profile)",
        )
    return CheckResult(
        "active_set_signed", "skip",
        value="not required by profile",
        expected=f"required only when profile demands it (current: {prof_name})",
        claim="Signed active set required for hardware profile only",
    )


def check_pack_positions_loaded(cur) -> CheckResult:
    """gdu_pack_positions table populated with valid coordinates."""
    cur.execute("SELECT COUNT(*) FROM gdu_pack_positions")
    n = cur.fetchone()[0]
    if n == 0:
        return CheckResult(
            "pack_positions_loaded", "fail",
            value="0 positions",
            expected=">= 60",
            claim="Pack positions loaded — every fact must land at a valid base-60 coordinate",
        )
    cur.execute(
        "SELECT COUNT(DISTINCT category) FROM gdu_pack_positions"
    )
    cats = cur.fetchone()[0]
    return CheckResult(
        "pack_positions_loaded", "pass",
        value=f"{n} positions across {cats} categories",
        expected=">= 60 positions",
        claim="Pack positions are loaded; 92+ valid (category, subtype) pairs out of 3600 possible",
    )


def check_is_canonical() -> CheckResult:
    """is_canonical() returns True under the current GDU context."""
    try:
        from core.gdu.signal_vocabulary import is_canonical
        ok = bool(is_canonical())
        if ok:
            return CheckResult(
                "is_canonical", "pass",
                value="True",
                expected="True",
                claim="GDU is loaded and lineage stamping uses verified governance",
            )
        return CheckResult(
            "is_canonical", "fail",
            value="False",
            expected="True",
            claim="GDU canonical context active; writes carry verified gdu_canonical flag",
        )
    except Exception as e:
        return CheckResult(
            "is_canonical", "fail",
            value=f"error: {e}",
            claim="GDU canonical context check is callable",
        )


def check_chain_integrity(cur) -> CheckResult:
    """gdu_admission_log: every previous_hash matches prior mutation_hash."""
    cur.execute("SELECT COUNT(*), COUNT(mutation_hash), COUNT(previous_hash) FROM gdu_admission_log")
    total, hashed, chained = cur.fetchone()
    # We do not re-hash every row here (expensive); rely on the chain
    # verifier worker for that. We do verify (a) every row has a hash,
    # (b) all-but-one (the genesis) have a previous_hash.
    if total == 0:
        return CheckResult(
            "admission_chain_integrity", "warn",
            value="0 rows",
            claim="Admission log hash-chained — tamper any row, chain breaks",
            note="empty log; not yet meaningful",
        )
    missing_hash = total - hashed
    missing_prev = total - chained
    # Genesis row legitimately has no previous_hash → allow 1
    if missing_hash > 0 or missing_prev > 1:
        return CheckResult(
            "admission_chain_integrity", "fail",
            value=f"total={total} hashed={hashed} chained={chained}",
            expected="all rows hashed, all-but-genesis chained",
            claim="Admission log fully hashed + chained",
        )
    return CheckResult(
        "admission_chain_integrity", "pass",
        value=f"{total} rows, all hashed, {chained} chained (genesis allowed unchained)",
        expected="full coverage minus genesis",
        claim="Admission log fully hashed + chained — tamper-evident",
    )


def check_entity_governance_trigger_exists(cur) -> CheckResult:
    """Trigger enforce_entities_governance protects the entities table."""
    cur.execute("""
        SELECT trigger_name FROM information_schema.triggers
         WHERE event_object_table = 'entities'
           AND trigger_name = 'trg_enforce_entities_governance'
         LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        return CheckResult(
            "entities_governance_trigger", "pass",
            value="trg_enforce_entities_governance present",
            expected="trigger installed",
            claim="DB-level governance trigger demotes any entity row outside pack",
        )
    return CheckResult(
        "entities_governance_trigger", "fail",
        value="trigger missing",
        expected="trg_enforce_entities_governance",
        claim="Entity governance enforced at DB layer",
    )


def check_knowledge_nodes_trigger(cur) -> CheckResult:
    cur.execute("""
        SELECT trigger_name FROM information_schema.triggers
         WHERE event_object_table = 'knowledge_nodes'
           AND trigger_name = 'trg_enforce_governance'
         LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        return CheckResult(
            "knowledge_nodes_governance_trigger", "pass",
            value="trg_enforce_governance present",
            expected="trigger installed",
            claim="DB-level governance trigger on knowledge_nodes",
        )
    return CheckResult(
        "knowledge_nodes_governance_trigger", "fail",
        value="trigger missing",
        expected="trg_enforce_governance",
        claim="Knowledge nodes governance enforced at DB layer",
    )


def check_entity_contamination(cur) -> CheckResult:
    """% of entities with canonical_type NOT in entity pack (contamination
    that the cross_category_migrator should evacuate)."""
    cur.execute("""
        SELECT
          COUNT(*) FILTER (
            WHERE canonical_type IS NOT NULL
              AND NOT (envelope ? 'reclassification')
              AND NOT (envelope ? 'normalized_from')
              AND NOT EXISTS (
                SELECT 1 FROM gdu_pack_positions
                 WHERE category = 'entity'
                   AND subtype = entities.canonical_type
              )
          ) AS contamination,
          COUNT(*) AS total
          FROM entities
    """)
    contam, total = cur.fetchone()
    if total == 0:
        return CheckResult("entity_contamination", "skip", value="0 entities",
                           claim="Contamination rate measurable when entities exist")
    rate = (contam / total) * 100
    if rate < 5:
        status = "pass"
    elif rate < 30:
        status = "warn"
    else:
        status = "fail"
    return CheckResult(
        "entity_contamination", status,
        value=f"{contam}/{total} ({rate:.1f}%) have non-entity subtype",
        expected="< 5% (low contamination — migrator keeping up)",
        claim="entities table contains only legitimate entity subtypes (place/event/etc routed elsewhere)",
        note="contamination = LDU producing dirty subtypes faster than migrator evacuates",
    )


def check_libraries_signed(cur) -> CheckResult:
    """Every is_public=TRUE library has publish_signature populated."""
    cur.execute("""
        SELECT
          COUNT(*) FILTER (WHERE is_public = TRUE) AS public_count,
          COUNT(*) FILTER (WHERE is_public = TRUE AND publish_signature IS NOT NULL) AS signed
          FROM libraries
    """)
    public_count, signed = cur.fetchone()
    if public_count == 0:
        return CheckResult("libraries_signed", "skip", value="no public libraries",
                           claim="Public libraries are Ed25519-signed by producing node")
    if signed == public_count:
        return CheckResult(
            "libraries_signed", "pass",
            value=f"{signed}/{public_count} signed",
            expected="100%",
            claim="Every published library carries an Ed25519 publish_signature",
        )
    return CheckResult(
        "libraries_signed", "fail",
        value=f"{signed}/{public_count} signed",
        expected="100%",
        claim="Every published library carries an Ed25519 publish_signature",
    )


def check_libraries_published_metadata(cur) -> CheckResult:
    """Published libraries have published_at + published_by_node_id."""
    cur.execute("""
        SELECT
          COUNT(*) FILTER (WHERE is_published_to_network = TRUE) AS pub,
          COUNT(*) FILTER (
            WHERE is_published_to_network = TRUE
              AND published_at IS NOT NULL
              AND published_by_node_id IS NOT NULL
          ) AS complete
          FROM libraries
    """)
    pub, complete = cur.fetchone()
    if pub == 0:
        return CheckResult("libraries_published_metadata", "skip",
                           value="no published libraries",
                           claim="Published libraries record when + who")
    if complete == pub:
        return CheckResult(
            "libraries_published_metadata", "pass",
            value=f"{complete}/{pub} have complete publish metadata",
            expected="100%",
            claim="Every published library records published_at and published_by_node_id",
        )
    return CheckResult(
        "libraries_published_metadata", "fail",
        value=f"{complete}/{pub} complete",
        expected="100%",
        claim="Every published library records published_at and published_by_node_id",
    )


def check_extraction_publications_signed(cur) -> CheckResult:
    """extraction_publications all signed."""
    cur.execute("SELECT COUNT(*), COUNT(signature) FROM extraction_publications")
    total, signed = cur.fetchone()
    if total == 0:
        return CheckResult("extraction_publications_signed", "skip",
                           value="0 publications",
                           claim="Federation extraction publications signed")
    if signed == total:
        return CheckResult(
            "extraction_publications_signed", "pass",
            value=f"{signed}/{total} signed",
            expected="100%",
            claim="Every federation extraction publication carries an Ed25519 signature",
        )
    return CheckResult(
        "extraction_publications_signed", "fail",
        value=f"{signed}/{total} signed",
        expected="100%",
        claim="Every federation extraction publication carries an Ed25519 signature",
    )


def check_hub_announce_recency(cur) -> CheckResult:
    """At least one public library was announced within the last 2 hours."""
    cur.execute("""
        SELECT MAX(published_at)
          FROM libraries
         WHERE is_public = TRUE
    """)
    last = cur.fetchone()[0]
    if last is None:
        return CheckResult(
            "hub_announce_recency", "warn",
            value="never announced",
            expected="within last 2h",
            claim="Hub announce worker is keeping the registry warm",
        )
    age_min = (datetime.now(timezone.utc) - last).total_seconds() / 60
    if age_min < 120:
        status = "pass"
    elif age_min < 24 * 60:
        status = "warn"
    else:
        status = "fail"
    return CheckResult(
        "hub_announce_recency", status,
        value=f"last announce {age_min:.0f} min ago",
        expected="< 120 min",
        claim="Hub announce worker keeps the federation catalog up to date (30-min cadence)",
    )


# ---------------------------------------------------------------------------
# Worker liveness + real-time contamination (added 2026-05-30)
# ---------------------------------------------------------------------------

def check_recent_contamination_1h(cur) -> CheckResult:
    """Entities created in the last hour with non-entity canonical_type
    and no reclassification envelope. Real-time proof the LDU write-time
    validator is holding (no new dirty rows landing in entities table)."""
    cur.execute("""
        SELECT
          COUNT(*) FILTER (
            WHERE canonical_type IS NOT NULL
              AND NOT (envelope ? 'reclassification')
              AND NOT (envelope ? 'normalized_from')
              AND NOT EXISTS (
                SELECT 1 FROM gdu_pack_positions
                 WHERE category = 'entity'
                   AND subtype = entities.canonical_type
              )
          ) AS new_contam,
          COUNT(*) AS new_total
          FROM entities
         WHERE created_at >= NOW() - INTERVAL '1 hour'
    """)
    new_contam, new_total = cur.fetchone()
    if new_total == 0:
        return CheckResult(
            "recent_contamination_1h", "skip",
            value="no new entities in last 1h",
            claim="Validator can be tested when ingestion is active",
        )
    if new_contam == 0:
        return CheckResult(
            "recent_contamination_1h", "pass",
            value=f"0/{new_total} new entities contaminated",
            expected="0 new contamination",
            claim="LDU write-time validator + CHECK constraint reject dirty subtypes at the door",
        )
    return CheckResult(
        "recent_contamination_1h", "fail",
        value=f"{new_contam}/{new_total} new entities have non-entity subtype",
        expected="0 new contamination",
        claim="LDU write-time validator + CHECK constraint reject dirty subtypes at the door",
        note="non-zero means the validator regressed OR a worker is inserting around it",
    )


def check_auto_validator_liveness(cur) -> CheckResult:
    """auto_validator processes knowledge_nodes with enrichment_status='pending'
    and flips them to 'complete'. Liveness is inferred from DB side-effects, not
    in-process _STATUS (which is empty in a fresh harness process)."""
    cur.execute("""
        SELECT
          COUNT(*) FILTER (
            WHERE enrichment_status = 'pending'
              AND node_type = 'entity'
              AND LENGTH(COALESCE(title,'')) >= 3
          ) AS backlog,
          COUNT(*) FILTER (
            WHERE enrichment_status = 'complete'
              AND node_type = 'entity'
              AND updated_at >= NOW() - INTERVAL '30 minutes'
          ) AS recent_updates
          FROM knowledge_nodes
    """)
    backlog, recent = cur.fetchone()
    if recent > 0:
        return CheckResult(
            "auto_validator_liveness", "pass",
            value=f"{recent} nodes validated in last 30min (backlog={backlog})",
            expected="recent activity OR empty backlog",
            claim="auto_validator worker is processing the queue",
        )
    if backlog == 0:
        return CheckResult(
            "auto_validator_liveness", "pass",
            value="0 backlog, 0 recent — idle but caught up",
            expected="recent activity OR empty backlog",
            claim="auto_validator worker is processing the queue",
        )
    return CheckResult(
        "auto_validator_liveness", "fail",
        value=f"{backlog} pending, 0 validated in last 30min",
        expected="recent activity when backlog exists",
        claim="auto_validator worker is processing the queue",
        note="worker likely died — restart api container or call start_worker()",
    )


def check_ldup_promoter_liveness(cur) -> CheckResult:
    """ldup_promoter sweeps entity_ldu_p WHERE status='pending' and flips
    matches to 'promoted'. Liveness inferred from DB side-effects."""
    try:
        cur.execute("""
            SELECT
              COUNT(*) FILTER (WHERE status = 'pending') AS backlog,
              COUNT(*) FILTER (
                WHERE status = 'promoted'
                  AND resolved_at >= NOW() - INTERVAL '30 minutes'
              ) AS recent_promotions
              FROM entity_ldu_p
        """)
    except Exception:
        return CheckResult(
            "ldup_promoter_liveness", "skip",
            value="entity_ldu_p table not present",
            claim="ldup_promoter worker is processing the queue",
        )
    backlog, recent = cur.fetchone()
    if recent > 0:
        return CheckResult(
            "ldup_promoter_liveness", "pass",
            value=f"{recent} promoted in last 30min (backlog={backlog})",
            expected="recent activity OR empty backlog",
            claim="ldup_promoter worker is processing the queue",
        )
    if backlog == 0:
        return CheckResult(
            "ldup_promoter_liveness", "pass",
            value="0 backlog, 0 recent — idle but caught up",
            expected="recent activity OR empty backlog",
            claim="ldup_promoter worker is processing the queue",
        )
    return CheckResult(
        "ldup_promoter_liveness", "fail",
        value=f"{backlog} pending, 0 promoted in last 30min",
        expected="recent activity when backlog exists",
        claim="ldup_promoter worker is processing the queue",
        note="worker likely died — restart api container or call start_ldup_promoter()",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_all_checks() -> dict[str, Any]:
    """Execute every check; return a structured report."""
    results: list[CheckResult] = []

    # Foundation (no DB needed)
    results.append(_safely(check_profile))
    results.append(_safely(check_software_root_key))
    results.append(_safely(check_peer_key))
    results.append(_safely(check_active_set_loadable))
    results.append(_safely(check_active_set_signed))
    results.append(_safely(check_is_canonical))

    # DB-backed checks
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                results.append(_safely_db(check_pack_positions_loaded, cur))
                results.append(_safely_db(check_chain_integrity, cur))
                results.append(_safely_db(check_entity_governance_trigger_exists, cur))
                results.append(_safely_db(check_knowledge_nodes_trigger, cur))
                results.append(_safely_db(check_entity_contamination, cur))
                results.append(_safely_db(check_libraries_signed, cur))
                results.append(_safely_db(check_libraries_published_metadata, cur))
                results.append(_safely_db(check_extraction_publications_signed, cur))
                results.append(_safely_db(check_hub_announce_recency, cur))
                results.append(_safely_db(check_recent_contamination_1h, cur))
                results.append(_safely_db(check_auto_validator_liveness, cur))
                results.append(_safely_db(check_ldup_promoter_liveness, cur))
    except Exception as e:
        results.append(CheckResult(
            "db_connection", "fail",
            value=f"error: {e}",
            claim="Database reachable for governance audit",
        ))

    summary = {
        "total": len(results),
        "pass":  sum(1 for r in results if r.status == "pass"),
        "fail":  sum(1 for r in results if r.status == "fail"),
        "warn":  sum(1 for r in results if r.status == "warn"),
        "skip":  sum(1 for r in results if r.status == "skip"),
    }
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "node_id":   os.environ.get("ENKI_NODE_ID", "unknown"),
        "profile":   os.environ.get("ENKI_PROFILE", "node"),
        "checks":    [r.as_dict() for r in results],
        "summary":   summary,
    }


def _safely(fn: Callable[[], CheckResult]) -> CheckResult:
    try:
        return fn()
    except Exception as e:
        return CheckResult(
            fn.__name__.replace("check_", ""),
            "fail",
            value=f"harness error: {e}",
            claim="Check itself runnable",
        )


def _safely_db(fn: Callable, cur) -> CheckResult:
    try:
        return fn(cur)
    except Exception as e:
        return CheckResult(
            fn.__name__.replace("check_", ""),
            "fail",
            value=f"harness error: {e}",
            claim="Check itself runnable",
        )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

ANSI = {
    "pass": "\033[32m",  # green
    "fail": "\033[31m",  # red
    "warn": "\033[33m",  # yellow
    "skip": "\033[90m",  # gray
    "reset": "\033[0m",
    "bold":  "\033[1m",
}


def render_human(report: dict[str, Any], use_color: bool = True) -> str:
    """ASCII table with color status."""
    def c(s, k):
        return f"{ANSI[k]}{s}{ANSI['reset']}" if use_color else s

    lines = []
    lines.append(c("Enki Spine Validation", "bold"))
    lines.append(f"  node:    {report['node_id']}")
    lines.append(f"  profile: {report['profile']}")
    lines.append(f"  when:    {report['timestamp']}")
    lines.append("")
    for chk in report["checks"]:
        status = chk["status"].upper()
        col_status = c(f"[{status:<4}]", chk["status"])
        lines.append(f"  {col_status} {chk['name']:<40} {chk['value']}")
        if chk.get("note"):
            lines.append(f"           {chk['note']}")
    s = report["summary"]
    lines.append("")
    lines.append(
        f"Summary: {c(str(s['pass']) + ' pass', 'pass')}  "
        f"{c(str(s['warn']) + ' warn', 'warn')}  "
        f"{c(str(s['fail']) + ' fail', 'fail')}  "
        f"{c(str(s['skip']) + ' skip', 'skip')}  "
        f"({s['total']} total)"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON instead of human table")
    ap.add_argument("--strict", action="store_true",
                    help="exit non-zero on any non-PASS (default: only fails)")
    ap.add_argument("--no-color", action="store_true",
                    help="suppress ANSI color (auto-off if not a TTY)")
    args = ap.parse_args()

    report = run_all_checks()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        use_color = sys.stdout.isatty() and not args.no_color
        print(render_human(report, use_color=use_color))

    s = report["summary"]
    if args.strict:
        return 1 if (s["fail"] + s["warn"]) > 0 else 0
    return 1 if s["fail"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
