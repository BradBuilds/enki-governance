"""
Federation peer authentication endpoints.

Implements the wire protocol for Phase 0 of the Library Federation plan
(the internal library federation plan).

Endpoints:
  GET  /fleet/peer/challenge              — fresh challenge nonce (any caller)
  POST /fleet/peer/handshake/initial      — invitation-token onboarding
  POST /fleet/peer/handshake/refresh      — JWT renewal via signed challenge
  GET  /fleet/peer/pubkey/{node_id}       — Phase 10 signature verification
  POST /fleet/peer/admin/invitations      — federation_admin issues a token
  POST /fleet/peer/admin/revoke           — federation_admin revokes a peer
  GET  /fleet/peer/admin/roster           — federation_admin lists members
  POST /fleet/peer/bootstrap              — one-shot install bootstrap

Auth model:
  - challenge / handshake_initial / handshake_refresh / bootstrap: unauthenticated
    (the handshake itself IS the auth)
  - admin endpoints: require_peer_role('federation_admin')
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from services.api.src import peer_auth
from services.api.src.unified_auth import (
    UnifiedAuthInfo,
    require_peer_role,
)
from storage.postgres.db import get_connection

log = logging.getLogger("enki.routes.peer_auth")
router = APIRouter(tags=["peer-auth"])


# ── Request models ────────────────────────────────────────────────────────


class HandshakeInitialReq(BaseModel):
    invitation_token: str
    peer_node_id: str = Field(min_length=1, max_length=255)
    pubkey_b64: str = Field(description="Ed25519 32-byte pubkey, base64-encoded")
    challenge_id: str
    signature_b64: str = Field(description="Ed25519 signature, base64-encoded")
    timestamp: int = Field(description="Unix seconds; must be within 60s of server time")
    metadata: Optional[dict] = None


class HandshakeRefreshReq(BaseModel):
    peer_node_id: str
    challenge_id: str
    signature_b64: str
    timestamp: int


class IssueInvitationReq(BaseModel):
    intended_for_label: Optional[str] = None
    initial_roles: Optional[list[str]] = None
    ttl_seconds: int = 7 * 24 * 3600


class RevokePeerReq(BaseModel):
    peer_node_id: str
    reason: Optional[str] = None


class BootstrapReq(BaseModel):
    pubkey_b64: str
    bootstrap_secret: str


# ── Public handshake endpoints ────────────────────────────────────────────


@router.get("/fleet/peer/challenge")
def get_challenge():
    """Get a fresh challenge nonce. Sign (challenge_id|peer_node_id|timestamp)
    with your Ed25519 key and POST to /fleet/peer/handshake/{initial,refresh}.
    Challenges expire after 60 seconds."""
    return peer_auth.issue_challenge()


@router.post("/fleet/peer/handshake/initial")
def handshake_initial(req: HandshakeInitialReq, request: Request):
    """First-time onboarding. Consumes an invitation token, registers the
    peer's pubkey, returns the initial JWT.

    Idempotent only in the failure sense — duplicate node_id or pubkey
    returns 409 without consuming the token."""
    return peer_auth.handshake_initial(
        invitation_token=req.invitation_token,
        peer_node_id=req.peer_node_id,
        pubkey_b64=req.pubkey_b64,
        challenge_id=req.challenge_id,
        signature_b64=req.signature_b64,
        timestamp=req.timestamp,
        metadata=req.metadata,
        request=request,
    )


@router.get("/fleet/peer/pubkey/{node_id}")
def get_peer_pubkey(node_id: str):
    """Public discovery: return the Ed25519 pubkey for a federation peer.
    Used by subscribers (Phase 10) to verify envelope signatures.
    Returns 404 if node_id isn't a registered active peer."""
    import base64
    from storage.postgres.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT pubkey_ed25519, pubkey_fingerprint, status, trust_score
                     FROM peer_memberships
                    WHERE peer_node_id = %s""",
                (node_id,),
            )
            row = cur.fetchone()
    if not row:
        raise HTTPException(404, f"peer {node_id} not registered")
    pubkey_bytes, fingerprint, status, trust = row
    if status != "active":
        raise HTTPException(404, f"peer {node_id} status={status}")
    return {
        "peer_node_id":       node_id,
        "pubkey_b64":         base64.b64encode(bytes(pubkey_bytes)).decode("ascii"),
        "pubkey_fingerprint": fingerprint,
        "trust_score":        float(trust),
    }


@router.post("/fleet/peer/handshake/refresh")
def handshake_refresh(req: HandshakeRefreshReq, request: Request):
    """Renew a JWT before it expires. Requires proof-of-key (signed
    challenge) but no invitation token."""
    return peer_auth.handshake_refresh(
        peer_node_id=req.peer_node_id,
        challenge_id=req.challenge_id,
        signature_b64=req.signature_b64,
        timestamp=req.timestamp,
        request=request,
    )


# ── Admin endpoints (federation_admin only) ───────────────────────────────


@router.post("/fleet/peer/admin/invitations")
def issue_invitation(
    req: IssueInvitationReq,
    request: Request,
    auth: UnifiedAuthInfo = Depends(require_peer_role("federation_admin")),
):
    """Mint a single-use invitation token. Shown once to the admin; deliver
    out-of-band to the prospective peer operator."""
    return peer_auth.issue_invitation(
        issued_by_node_id=auth.subject_id,
        initial_roles=req.initial_roles,
        intended_for_label=req.intended_for_label,
        ttl_seconds=req.ttl_seconds,
        request=request,
    )


@router.post("/fleet/peer/admin/revoke")
def revoke_peer(
    req: RevokePeerReq,
    request: Request,
    auth: UnifiedAuthInfo = Depends(require_peer_role("federation_admin")),
):
    """Revoke a peer. Existing JWTs continue until TTL expires (max 1h).
    Next refresh will fail; new publish/consume blocked."""
    return peer_auth.revoke_peer(
        peer_node_id=req.peer_node_id,
        revoked_by=auth.subject_id,
        reason=req.reason,
        request=request,
    )


@router.get("/fleet/peer/admin/roster")
def list_peers(
    auth: UnifiedAuthInfo = Depends(require_peer_role("federation_admin")),
):
    """List all federation members with status + roles + last_seen."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT peer_node_id, pubkey_fingerprint, status, trust_score,
                       roles, invited_by_node_id, activated_at, last_seen_at,
                       metadata
                  FROM peer_memberships
                 ORDER BY activated_at DESC NULLS LAST, peer_node_id
                """
            )
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # Format timestamps + skip the bootstrap row in normal listings (optional)
    for r in rows:
        for k in ("activated_at", "last_seen_at"):
            if r.get(k) is not None:
                r[k] = r[k].isoformat()
    return {"peers": rows, "count": len(rows)}


# ── Bootstrap (one-shot, secured by env-secret only) ──────────────────────


@router.post("/fleet/peer/bootstrap")
def bootstrap_admin(req: BootstrapReq, request: Request):
    """One-shot endpoint consumed by the install script on first hub boot.
    Activates the bootstrap_admin row with the installer's pubkey, returns
    a federation_admin JWT. Subsequent calls return 409.

    Sealing: after the install script consumes this, it MUST rotate
    ENKI_HUB_BOOTSTRAP_SECRET in /etc/enki/hub.env to a fresh value so this
    endpoint can never be invoked again with the original secret."""
    return peer_auth.bootstrap_admin(
        pubkey_b64=req.pubkey_b64,
        bootstrap_secret=req.bootstrap_secret,
        request=request,
    )
