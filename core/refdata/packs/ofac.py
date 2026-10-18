"""
OFAC SDN refdata pack — downloads the Treasury Specially Designated Nationals
CSV bundle (sdn.csv + alt.csv + add.csv) and indexes it into refdata.ofac_sdn,
refdata.ofac_sdn_aliases, refdata.ofac_sdn_addresses.

Replaces the per-call live CSV download in
core/investigation/sources.py::investigate_ofac. After first sync the
investigate_ofac handler consults this module's lookup() and runs a PG query
instead of fetching ~25 MB of CSV every call.

OFAC CSV format (no headers, comma-separated, all fields quoted with `"`):

  sdn.csv:  ent_num, sdn_name, sdn_type, program, title, call_sign, vess_type,
            tonnage, grt, vess_flag, vess_owner, remarks
  alt.csv:  ent_num, alt_num, alt_type, alt_name, alt_remarks
  add.csv:  ent_num, add_num, address, city_state_postal, country, add_remarks

Quote-escaping: a literal " inside a field is escaped by doubling ("").
Fields containing a `,` are quoted; otherwise quoting is consistent so a naive
parser using csv.reader works.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from storage.postgres.db import get_connection
from core.refdata.manager import fetch_bytes

log = logging.getLogger("enki.refdata.packs.ofac")

_PUNCT_RE = re.compile(r"[^A-Z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def _normalize(name: str) -> str:
    """Match-friendly normalization: uppercase, strip punctuation, collapse
    whitespace. Aligned with how the source handler builds its query."""
    if not name:
        return ""
    s = name.upper()
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def _read_csv_rows(body: bytes) -> list[list[str]]:
    """OFAC CSV uses field separators of `,` with all fields quoted in `"`.
    csv.reader handles this. Decode as latin-1 to survive non-UTF-8 chars
    occasionally present in transliterated names."""
    text = body.decode("latin-1", errors="replace")
    return list(csv.reader(io.StringIO(text), delimiter=",", quotechar='"'))


def run(pack_def: dict, refdata_home: Path) -> dict:
    """Download + parse + index. Returns the outcome dict the manager records."""
    pack_id = pack_def["pack_id"]
    urls = pack_def.get("source_urls", {})
    sdn_url = urls.get("primary")
    alt_url = urls.get("aliases")
    add_url = urls.get("addresses")
    if not (sdn_url and alt_url and add_url):
        raise RuntimeError("OFAC pack manifest missing one of primary/aliases/addresses URLs")

    out_dir = refdata_home / "ofac"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Download all three CSVs
    sdn_body, sdn_sha = fetch_bytes(sdn_url)
    alt_body, alt_sha = fetch_bytes(alt_url)
    add_body, add_sha = fetch_bytes(add_url)

    # Persist raw for audit / re-index
    (out_dir / "sdn.csv").write_bytes(sdn_body)
    (out_dir / "alt.csv").write_bytes(alt_body)
    (out_dir / "add.csv").write_bytes(add_body)

    version = datetime.utcnow().strftime("%Y-%m-%d")
    total_bytes = len(sdn_body) + len(alt_body) + len(add_body)
    combined_sha = sdn_sha[:16] + alt_sha[:16] + add_sha[:16]

    # Parse
    sdn_rows = _read_csv_rows(sdn_body)
    alt_rows = _read_csv_rows(alt_body)
    add_rows = _read_csv_rows(add_body)

    log.info(
        "OFAC parsed sdn=%d alt=%d add=%d",
        len(sdn_rows), len(alt_rows), len(add_rows),
    )

    # Index — full replacement strategy. Wipe tables, repopulate atomically
    # so source handlers never see a half-indexed snapshot.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE refdata.ofac_sdn CASCADE")
            # SDN main rows
            sdn_insert = []
            for r in sdn_rows:
                if len(r) < 4:
                    continue
                try:
                    uid = int(r[0])
                except (TypeError, ValueError):
                    continue
                name = (r[1] or "").strip()
                sdn_type = (r[2] or "").strip()
                program = (r[3] or "").strip()
                title = (r[4] if len(r) > 4 else "") or ""
                call_sign = (r[5] if len(r) > 5 else "") or ""
                vess_type = (r[6] if len(r) > 6 else "") or ""
                tonnage = (r[7] if len(r) > 7 else "") or ""
                grt = (r[8] if len(r) > 8 else "") or ""
                vess_flag = (r[9] if len(r) > 9 else "") or ""
                vess_owner = (r[10] if len(r) > 10 else "") or ""
                remarks = (r[11] if len(r) > 11 else "") or ""
                sdn_insert.append((
                    uid, name, _normalize(name), sdn_type, program, title.strip(),
                    remarks.strip(), call_sign.strip(), vess_type.strip(),
                    tonnage.strip(), grt.strip(), vess_flag.strip(),
                    vess_owner.strip(), version,
                ))

            # psycopg2.executemany — for a real bulk load we'd use COPY; this
            # is fine at ~13K rows. Future optimization: COPY FROM STDIN.
            cur.executemany(
                """
                INSERT INTO refdata.ofac_sdn
                    (uid, name, name_normalized, sdn_type, program, title,
                     remarks, call_sign, vess_type, tonnage, grt, vess_flag,
                     vess_owner, pack_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (uid) DO UPDATE SET
                    name = EXCLUDED.name,
                    name_normalized = EXCLUDED.name_normalized,
                    sdn_type = EXCLUDED.sdn_type,
                    program = EXCLUDED.program,
                    title = EXCLUDED.title,
                    remarks = EXCLUDED.remarks,
                    pack_version = EXCLUDED.pack_version
                """,
                sdn_insert,
            )

            # Aliases
            alt_insert = []
            for r in alt_rows:
                if len(r) < 4:
                    continue
                try:
                    uid = int(r[0])
                    alias_id = int(r[1])
                except (TypeError, ValueError):
                    continue
                alias_type = (r[2] or "").strip()
                alias_name = (r[3] or "").strip()
                alt_insert.append((
                    alias_id, uid, alias_type, "strong",
                    alias_name, _normalize(alias_name),
                ))
            cur.executemany(
                """
                INSERT INTO refdata.ofac_sdn_aliases
                    (alias_id, uid, alias_type, alias_category,
                     alias_name, alias_normalized)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (alias_id, uid) DO NOTHING
                """,
                alt_insert,
            )

            # Addresses
            add_insert = []
            for r in add_rows:
                if len(r) < 3:
                    continue
                try:
                    uid = int(r[0])
                    addr_id = int(r[1])
                except (TypeError, ValueError):
                    continue
                address = (r[2] or "").strip()
                city_state = (r[3] if len(r) > 3 else "") or ""
                country = (r[4] if len(r) > 4 else "") or ""
                add_insert.append((
                    addr_id, uid, address, city_state.strip(), country.strip(),
                ))
            cur.executemany(
                """
                INSERT INTO refdata.ofac_sdn_addresses
                    (addr_id, uid, address, city_state, country)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (addr_id, uid) DO NOTHING
                """,
                add_insert,
            )
        conn.commit()

    return {
        "records_indexed": len(sdn_insert),
        "aliases_indexed": len(alt_insert),
        "addresses_indexed": len(add_insert),
        "bytes_downloaded": total_bytes,
        "sha256": combined_sha,
        "version": version,
        "on_disk_path": str(out_dir),
    }


# ─── Lookup — called by core/investigation/sources.py::investigate_ofac ──

def lookup(name: str, limit: int = 5) -> list[dict]:
    """Local-first lookup. Returns up to `limit` SDN matches scored by name
    + alias overlap. Same shape the source handler already emits, so its
    finding-builder doesn't need to change.

    Scoring:
      - 0.95: full normalized name appears in SDN name or alias
      - 0.85: all tokens of normalized name appear in SDN name (any order)
      - 0.70: last-token match only (catches surname-only hits)
    """
    normalized = _normalize(name)
    if not normalized or len(normalized) < 2:
        return []

    tokens = [t for t in normalized.split() if t]
    last_token = tokens[-1] if tokens else normalized

    # First: trigram-similarity ordered candidates, then refine in Python so
    # we keep scoring logic auditable + adjustable without DB function deploys.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT uid, name, sdn_type, program, title, remarks, name_normalized
                FROM refdata.ofac_sdn
                WHERE name_normalized %% %s
                   OR uid IN (
                       SELECT uid FROM refdata.ofac_sdn_aliases
                       WHERE alias_normalized %% %s
                   )
                ORDER BY GREATEST(
                    similarity(name_normalized, %s),
                    COALESCE((SELECT MAX(similarity(alias_normalized, %s))
                              FROM refdata.ofac_sdn_aliases
                              WHERE refdata.ofac_sdn_aliases.uid = refdata.ofac_sdn.uid), 0)
                ) DESC
                LIMIT %s
                """,
                (normalized, normalized, normalized, normalized, limit * 4),
            )
            candidates = cur.fetchall()

    out = []
    for c in candidates:
        uid, sdn_name, sdn_type, program, title, remarks, sdn_norm = c
        score = 0.0
        if normalized in sdn_norm:
            score = 0.95
        elif tokens and all(t in sdn_norm for t in tokens):
            score = 0.85
        elif last_token and last_token in sdn_norm:
            score = 0.70

        if score == 0.0:
            # Alias path — pull the strongest alias match
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT alias_name, alias_normalized
                        FROM refdata.ofac_sdn_aliases
                        WHERE uid = %s
                        ORDER BY similarity(alias_normalized, %s) DESC
                        LIMIT 1
                        """,
                        (uid, normalized),
                    )
                    row = cur.fetchone()
                    if row:
                        alias_name, alias_norm = row
                        if normalized in alias_norm:
                            score = 0.92
                        elif tokens and all(t in alias_norm for t in tokens):
                            score = 0.82

        if score >= 0.70:
            out.append({
                "uid": uid,
                "name": sdn_name,
                "type": sdn_type,
                "program": program,
                "title": title,
                "remarks": remarks,
                "score": score,
                "source_list": "OFAC SDN",
                "via": "refdata",
            })

    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]
