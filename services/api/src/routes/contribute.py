"""
Open Contribution — anyone can submit source documents to a library.

Three submission paths, all public (no hard auth requirement). Every
submission creates a permanent provenance mark (who added it) + source link
that stays visible forever, even if the doc is later flagged off-topic.

Tiers & rules (see migration 129):

  identified (logged-in / device-signed)
      url|upload → server fetches + ingests, normal weight, routed to the
      relevance gate. Easy path for regular people.

  anonymous WITHOUT compute-proof
      url|upload → HELD as `pending_self_validation`. We do NOT spend our
      compute on unaccountable submissions. Caller is told to self-validate.

  anonymous WITH compute-proof
      envelope → a signed, content-addressed extraction envelope the
      contributor produced on THEIR OWN compute. We verify (signature +
      content hash) and apply WITHOUT re-extracting, at LOW weight, parked
      until corroborated. This is the spam-resistant "offer compute" tier.

  system:acquirer
      The automated acquirer is registered as a contributor too, so it is
      subject to the same relevance gate (this is the contamination fix).

Weight model: contribution.weight = base(kind) * contributor.trust_score.
Promotion of derived facts is gated on weight in Phase 4
(core/documents/assessment_promoter.py) — low-weight sources stay at the
`observation` tier until corroborated.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from pydantic import BaseModel

from storage.postgres.db import get_connection

log = logging.getLogger("enki.routes.contribute")

router = APIRouter(prefix="/public/contribute", tags=["contribute"])

# Permanent store for contributed uploads (acquirer uses /tmp/enki_acquired; we
# want contributions to survive, so a dedicated dir under the data root).
_CONTRIB_ROOT = Path(os.environ.get("ENKI_CONTRIB_ROOT", "/var/lib/enki/contributions"))

# base weight per tier; effective weight = base * contributor.trust_score (0.5 prior)
_BASE_WEIGHT = {"identified": 2.0, "system": 1.2, "anonymous": 0.6}
# Phase 4 uses this threshold to decide whether a source can promote to `fact`.
PROMOTE_WEIGHT_THRESHOLD = 0.5


# ── identity resolution (optional auth — public router) ───────────────────────
async def _resolve_contributor(request: Request) -> tuple[str, str, Optional[str]]:
    """Return (contributor_id, kind, display_name). Identified if the request
    carries valid auth; anonymous otherwise. Never raises on missing auth."""
    try:
        from services.api.src.unified_auth import unified_auth
        info = await unified_auth(request)
        if info and info.subject_id and info.subject_type != "dev":
            name = info.name or info.email or info.subject_id
            return (f"user:{info.subject_id}", "identified", name)
        if info and info.subject_type == "dev":
            # dev profile = treat as identified local operator
            return (f"user:{info.subject_id}", "identified", info.name or "operator")
    except Exception:
        pass
    return ("", "anonymous", None)


def _upsert_contributor(contributor_id: str, kind: str, display_name: Optional[str],
                         pubkey: Optional[str] = None) -> float:
    """Ensure a contributors row exists; return its trust_score."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contributors (contributor_id, kind, display_name, pubkey)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (contributor_id) DO UPDATE SET
                    last_seen_at = NOW(),
                    display_name = COALESCE(EXCLUDED.display_name, contributors.display_name),
                    pubkey       = COALESCE(EXCLUDED.pubkey, contributors.pubkey)
                RETURNING trust_score
                """,
                (contributor_id, kind, display_name, pubkey),
            )
            trust = float(cur.fetchone()[0])
        conn.commit()
    return trust


def _weight(kind: str, trust_score: float) -> float:
    return round(_BASE_WEIGHT.get(kind, 0.6) * trust_score, 4)


def _library_exists(library_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT library_id, canonical_name, focal_term FROM libraries WHERE library_id=%s",
                (library_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, "library not found")
    return {"library_id": row[0], "canonical_name": row[1], "focal_term": row[2]}


def _record_contribution(*, library_id: int, contributor_id: str, kind: str,
                          input_kind: str, source_url: Optional[str],
                          upload_path: Optional[str], content_sha256: Optional[str],
                          status: str, weight: float, signature: Optional[str] = None,
                          envelope_sha256: Optional[str] = None,
                          document_id: Optional[int] = None) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO contributions
                    (library_id, contributor_id, document_id, kind, input_kind,
                     source_url, upload_path, content_sha256, signature,
                     envelope_sha256, status, weight)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING contribution_id
                """,
                (library_id, contributor_id, document_id, kind, input_kind,
                 source_url, upload_path, content_sha256, signature,
                 envelope_sha256, status, weight),
            )
            cid = cur.fetchone()[0]
            cur.execute(
                "UPDATE contributors SET total_submissions = total_submissions + 1 WHERE contributor_id=%s",
                (contributor_id,),
            )
        conn.commit()
    return cid


def _attach_document(*, library_id: int, document_id: int, contributor_id: str,
                      contribution_id: int, weight: float, source_url: Optional[str]):
    """Stamp the doc with its contributor mark + link, and link it to the
    library (the membership carries the weight + 'pending' until the gate runs)."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE documents
                      SET contributor_id=%s, contribution_id=%s,
                          source_url=COALESCE(source_url, %s)
                    WHERE document_id=%s""",
                (contributor_id, contribution_id, source_url, document_id),
            )
            cur.execute(
                """
                INSERT INTO library_members
                    (library_id, member_kind, member_id, added_by,
                     contributor_id, contributor_weight, submission_status)
                VALUES (%s, 'document', %s, %s, %s, %s, 'pending')
                ON CONFLICT (library_id, member_kind, member_id) DO UPDATE SET
                    contributor_id     = EXCLUDED.contributor_id,
                    contributor_weight = EXCLUDED.contributor_weight,
                    submission_status  = 'pending'
                """,
                (library_id, document_id, contributor_id, contributor_id, weight),
            )
            cur.execute(
                "UPDATE contributions SET document_id=%s, status='validating', updated_at=NOW() WHERE contribution_id=%s",
                (document_id, contribution_id),
            )
        conn.commit()


def _ingest_to_doc(*, path: str, library: dict, source_url: Optional[str]) -> int:
    """Run the existing PDF ingest pipeline → returns document_id. Extraction
    (LLM mentions/statements) is deferred to the relevance gate + collapse worker."""
    from core.documents.pdf_ingester import ingest_pdf
    res = ingest_pdf(
        path=path,
        dataset_id=f"contributions:{library['library_id']}",
        title=None,
        source_url=source_url,
        trust_class="mentioned",
    )
    if not res.get("ok"):
        raise HTTPException(422, f"ingest failed: {res.get('error')}")
    return int(res["document_id"])


# ── Libraries open for contribution (public picker) ──────────────────────────
@router.get("/libraries")
def contribute_libraries():
    """Public list of libraries anyone can contribute to."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT library_id, canonical_name, focal_term, description,
                          document_count, entity_count
                     FROM libraries
                    WHERE discovery_status = 'active'
                    ORDER BY document_count DESC NULLS LAST
                    LIMIT 100""")
            rows = cur.fetchall()
    return {"libraries": [
        {"library_id": r[0], "name": r[1], "focal_term": r[2], "description": r[3],
         "document_count": r[4] or 0, "entity_count": r[5] or 0}
        for r in rows]}


# ── URL submission ────────────────────────────────────────────────────────────
class UrlSubmission(BaseModel):
    library_id: int
    source_url: str
    display_name: Optional[str] = None


@router.post("/url")
async def contribute_url(body: UrlSubmission, request: Request):
    lib = _library_exists(body.library_id)
    contributor_id, kind, display_name = await _resolve_contributor(request)
    display_name = display_name or body.display_name

    if kind == "anonymous":
        # Held: anonymous submissions must be self-validated via own compute.
        anon_id = "anon:" + hashlib.sha256(body.source_url.encode()).hexdigest()[:24]
        _upsert_contributor(anon_id, "anonymous", display_name)
        cid = _record_contribution(
            library_id=body.library_id, contributor_id=anon_id, kind="anonymous",
            input_kind="url", source_url=body.source_url, upload_path=None,
            content_sha256=None, status="pending_self_validation", weight=0.0)
        return {
            "contribution_id": cid, "status": "pending_self_validation",
            "message": ("Anonymous submissions must be validated on your own "
                        "compute. Run the ENKI contributor client on this "
                        "document and submit the signed envelope to "
                        "/public/contribute/envelope."),
        }

    # Identified: server fetches + ingests.
    trust = _upsert_contributor(contributor_id, kind, display_name)
    weight = _weight(kind, trust)
    cid = _record_contribution(
        library_id=body.library_id, contributor_id=contributor_id, kind=kind,
        input_kind="url", source_url=body.source_url, upload_path=None,
        content_sha256=None, status="received", weight=weight)

    dest = _CONTRIB_ROOT / str(body.library_id) / str(cid)
    dest.mkdir(parents=True, exist_ok=True)
    ref = {"identifiers": {"url": body.source_url}, "title": None}
    from core.discovery.acquirer import _attempt_direct_url, _attempt_archive_org
    result = _attempt_direct_url(ref, dest) or _attempt_archive_org(ref, dest)
    if not result:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE contributions SET status='rejected', error='download_failed' WHERE contribution_id=%s", (cid,))
            conn.commit()
        raise HTTPException(422, "could not download a document from that URL")

    doc_id = _ingest_to_doc(path=result["file_path"], library=lib, source_url=body.source_url)
    _attach_document(library_id=body.library_id, document_id=doc_id,
                     contributor_id=contributor_id, contribution_id=cid,
                     weight=weight, source_url=body.source_url)
    return {"contribution_id": cid, "document_id": doc_id, "status": "validating",
            "contributor": contributor_id, "weight": weight,
            "message": "Document ingested. Relevance gate will score it shortly."}


# ── File upload submission ────────────────────────────────────────────────────
@router.post("/upload")
async def contribute_upload(request: Request,
                            library_id: int = Form(...),
                            file: UploadFile = File(...),
                            display_name: Optional[str] = Form(None)):
    lib = _library_exists(library_id)
    contributor_id, kind, dn = await _resolve_contributor(request)
    display_name = dn or display_name

    raw = await file.read()
    if len(raw) > 200_000_000:
        raise HTTPException(413, "file too large (200MB max)")
    sha = hashlib.sha256(raw).hexdigest()

    if kind == "anonymous":
        anon_id = "anon:" + sha[:24]
        _upsert_contributor(anon_id, "anonymous", display_name)
        cid = _record_contribution(
            library_id=library_id, contributor_id=anon_id, kind="anonymous",
            input_kind="upload", source_url=None, upload_path=None,
            content_sha256=sha, status="pending_self_validation", weight=0.0)
        return {"contribution_id": cid, "status": "pending_self_validation",
                "message": ("Anonymous uploads must be validated on your own "
                            "compute. Run the ENKI contributor client and submit "
                            "the signed envelope to /public/contribute/envelope.")}

    trust = _upsert_contributor(contributor_id, kind, display_name)
    weight = _weight(kind, trust)
    cid = _record_contribution(
        library_id=library_id, contributor_id=contributor_id, kind=kind,
        input_kind="upload", source_url=None, upload_path=None,
        content_sha256=sha, status="received", weight=weight)

    dest = _CONTRIB_ROOT / str(library_id) / str(cid)
    dest.mkdir(parents=True, exist_ok=True)
    fname = (file.filename or "upload.pdf").replace("/", "_")
    if not fname.lower().endswith(".pdf"):
        fname += ".pdf"
    target = dest / fname
    target.write_bytes(raw)
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE contributions SET upload_path=%s WHERE contribution_id=%s", (str(target), cid))
        conn.commit()

    doc_id = _ingest_to_doc(path=str(target), library=lib, source_url=None)
    _attach_document(library_id=library_id, document_id=doc_id,
                     contributor_id=contributor_id, contribution_id=cid,
                     weight=weight, source_url=None)
    return {"contribution_id": cid, "document_id": doc_id, "status": "validating",
            "contributor": contributor_id, "weight": weight}


# ── Compute-proof envelope submission (anonymous, spam-resistant) ─────────────
class EnvelopeSubmission(BaseModel):
    library_id: int
    document_id: int
    content_sha256: str
    envelope_b64: str          # base64 of the canonical envelope JSON bytes
    signature: str             # base64 Ed25519 signature over envelope_sha256
    contributor_pubkey: str    # hex Ed25519 pubkey


@router.post("/envelope")
async def contribute_envelope(body: EnvelopeSubmission):
    """Accept a contributor-produced, signed extraction envelope. We VERIFY
    (signature + content hash + structural gates) and APPLY without re-running
    any LLM. Admitted at LOW weight, parked until corroborated."""
    _library_exists(body.library_id)
    try:
        envelope_bytes = base64.b64decode(body.envelope_b64)
    except Exception:
        raise HTTPException(400, "envelope_b64 not valid base64")

    # 1) structural + content-hash gates (reuse the federation verifier)
    from core.workers.extraction_subscriber import _verify_envelope, _apply_extraction_envelope
    envelope, gate_err = _verify_envelope(envelope_bytes, body.content_sha256, None)
    if gate_err:
        raise HTTPException(422, f"envelope rejected: {gate_err.get('code', gate_err)}")

    # 2) contributor signature gate (proves possession of the key)
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(body.contributor_pubkey))
        pub.verify(base64.b64decode(body.signature),
                   envelope["envelope_sha256"].encode("utf-8"))
    except InvalidSignature:
        raise HTTPException(401, "contributor signature invalid")
    except Exception as e:
        raise HTTPException(400, f"signature verification error: {e}")

    anon_id = "anon:" + hashlib.sha256(bytes.fromhex(body.contributor_pubkey)).hexdigest()[:24]
    trust = _upsert_contributor(anon_id, "anonymous", None, pubkey=body.contributor_pubkey)
    weight = _weight("anonymous", trust)

    # 3) apply (no re-extraction), tag provenance to the contributor
    apply_res = _apply_extraction_envelope(
        envelope=envelope, document_id=body.document_id,
        producer_node_id=anon_id, tenant_id="default")

    cid = _record_contribution(
        library_id=body.library_id, contributor_id=anon_id, kind="anonymous",
        input_kind="envelope", source_url=None, upload_path=None,
        content_sha256=body.content_sha256, status="validating", weight=weight,
        signature=body.signature, envelope_sha256=envelope["envelope_sha256"],
        document_id=body.document_id)
    _attach_document(library_id=body.library_id, document_id=body.document_id,
                     contributor_id=anon_id, contribution_id=cid,
                     weight=weight, source_url=None)
    return {"contribution_id": cid, "status": "validating", "applied": apply_res,
            "contributor": anon_id, "weight": weight,
            "message": "Signed envelope verified and applied at low weight (parked until corroborated)."}


# ── Status lookup ─────────────────────────────────────────────────────────────
@router.get("/status/{contribution_id}")
def contribution_status(contribution_id: int):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT c.status, c.kind, c.input_kind, c.source_url, c.weight,
                          c.relevance_score, c.relevance_reason, c.document_id,
                          c.contributor_id, ct.trust_score, d.relevance_status
                     FROM contributions c
                     JOIN contributors ct ON ct.contributor_id = c.contributor_id
                     LEFT JOIN documents d ON d.document_id = c.document_id
                    WHERE c.contribution_id = %s""",
                (contribution_id,),
            )
            r = cur.fetchone()
    if not r:
        raise HTTPException(404, "contribution not found")
    return {
        "contribution_id": contribution_id, "status": r[0], "kind": r[1],
        "input_kind": r[2], "source_url": r[3], "weight": r[4],
        "relevance_score": r[5], "relevance_reason": r[6], "document_id": r[7],
        "contributor_id": r[8], "contributor_trust": r[9], "relevance_status": r[10],
    }
