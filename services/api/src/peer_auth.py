"""
Peer-to-peer federation authentication.

This is Phase 0 of the Library Federation plan
(the internal library federation plan). Before this module
existed, hub auth was a single HTTP header (X-Enki-Node-Id, self-asserted)
which made every cross-node trust gate decorative.

The model:
  1. Each peer holds an Ed25519 keypair (generated at install, kept at
     /var/lib/enki/peer_key.bin permission 0600).
  2. Hub admin issues a single-use invitation_token to onboard a new peer.
  3. New peer fetches a challenge from the hub, signs (challenge_id |
     peer_node_id | timestamp) with its private key, POSTs the signature
     plus the invitation_token. Hub verifies the signature against the
     pubkey, consumes the token, inserts peer_memberships, returns a JWT.
  4. Peer caches the JWT, includes it as Authorization: Bearer on every
     subsequent hub call.
  5. JWT expires; peer refreshes by signing a fresh challenge (no
     invitation token required for refresh — just proof-of-key).
  6. Hub admin can revoke any peer; next JWT refresh fails.

JWT signing: HS256 with hub-side secret `ENKI_PEER_JWT_SECRET`.
JWT TTL: 1 hour default (configurable via ENKI_PEER_JWT_TTL_SECONDS).
Challenge TTL: 60 seconds (signed challenges must be fresh).

All paths log to peer_auth_audit; revocation is immediate (no JWT
blacklist needed because TTL is short — at most 1 hour to propagate).

Endpoints (mounted in routes/peer_auth.py):
  POST /fleet/peer/handshake/initial   — invitation flow
  POST /fleet/peer/handshake/refresh   — JWT renewal
  GET  /fleet/peer/challenge           — fresh challenge nonce
  POST /fleet/peer/bootstrap           — one-shot install bootstrap

Dual-mode: when ENKI_AUTH_MODE='dual' (default), endpoints accept either
peer JWT or legacy X-Enki-Node-Id header. When ENKI_AUTH_MODE='required',
header fallback is rejected.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request

from storage.postgres.db import get_connection

log = logging.getLogger("enki.peer_auth")

# ── Configuration ──────────────────────────────────────────────────────────

JWT_ALG = "HS256"
JWT_TTL_S = int(os.environ.get("ENKI_PEER_JWT_TTL_SECONDS", "3600"))
CHALLENGE_TTL_S = 60
AUTH_MODE = os.environ.get("ENKI_AUTH_MODE", "dual")  # 'dual' | 'required'


def _jwt_secret() -> str:
    """Hub's symmetric JWT signing key. Required in hub deployments; the
    install script generates and stores it in /etc/enki/hub.env."""
    secret = os.environ.get("ENKI_PEER_JWT_SECRET", "").strip()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail=(
                "Federation peer auth not configured: set ENKI_PEER_JWT_SECRET. "
                "The install script normally writes this to /etc/enki/hub.env."
            ),
        )
    return secret


# ── Data classes ──────────────────────────────────────────────────────────


@dataclass
class PeerAuthClaims:
    """Resolved JWT claims for an authenticated peer request."""
    peer_node_id: str
    roles: list[str]
    trust_score: float
    issued_at: int
    expires_at: int

    def has_role(self, role: str) -> bool:
        return role in self.roles


# ── Audit helper ──────────────────────────────────────────────────────────


def _audit(
    *,
    event: str,
    peer_node_id: Optional[str] = None,
    reason: Optional[str] = None,
    request: Optional[Request] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Append-only write to peer_auth_audit. Never raises — auditing must
    not break the request path."""
    try:
        ip = None
        if request is not None and request.client is not None:
            ip = request.client.host
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO peer_auth_audit
                        (peer_node_id, event, reason, request_ip, metadata)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    """,
                    (peer_node_id, event, reason, ip,
                     json.dumps(metadata or {})),
                )
            conn.commit()
    except Exception as e:
        log.warning("audit write failed (event=%s peer=%s): %s", event, peer_node_id, e)


# ── Challenge store ───────────────────────────────────────────────────────
# In-memory map of {challenge_id: (nonce, issued_at)}. Hub is single-process
# per replica; for multi-replica hubs, swap to Redis (deferred).


_CHALLENGES: dict[str, tuple[str, float]] = {}


def issue_challenge() -> dict:
    """Mint a fresh challenge nonce. Caller signs it to prove key possession.
    Challenges auto-expire after CHALLENGE_TTL_S."""
    _gc_challenges()
    challenge_id = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(32)
    _CHALLENGES[challenge_id] = (nonce, time.time())
    return {"challenge_id": challenge_id, "nonce": nonce, "ttl": CHALLENGE_TTL_S}


def _gc_challenges() -> None:
    """Drop stale challenges. Called opportunistically; cheap."""
    cutoff = time.time() - CHALLENGE_TTL_S
    stale = [k for k, (_, ts) in _CHALLENGES.items() if ts < cutoff]
    for k in stale:
        _CHALLENGES.pop(k, None)


def _consume_challenge(challenge_id: str) -> Optional[str]:
    """Pop a challenge by id. Returns the nonce, or None if missing/stale."""
    _gc_challenges()
    entry = _CHALLENGES.pop(challenge_id, None)
    if entry is None:
        return None
    nonce, issued_at = entry
    if time.time() - issued_at > CHALLENGE_TTL_S:
        return None
    return nonce


# ── Ed25519 helpers ───────────────────────────────────────────────────────


def _verify_ed25519(
    pubkey_bytes: bytes,
    message: bytes,
    signature_b64: str,
) -> bool:
    """Verify base64-encoded Ed25519 signature. Returns False on any error."""
    try:
        signature = base64.b64decode(signature_b64)
        pubkey = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        pubkey.verify(signature, message)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def _pubkey_fingerprint(pubkey_bytes: bytes) -> str:
    return hashlib.sha256(pubkey_bytes).hexdigest()


# ── Handshake: initial (invitation token) ─────────────────────────────────


def handshake_initial(
    *,
    invitation_token: str,
    peer_node_id: str,
    pubkey_b64: str,
    challenge_id: str,
    signature_b64: str,
    timestamp: int,
    metadata: Optional[dict] = None,
    request: Optional[Request] = None,
) -> dict:
    """First-time handshake. Validates invitation token, verifies signature
    against the presented pubkey, inserts peer_memberships, returns JWT.

    Signed message format (canonical):
        f"{challenge_id}|{peer_node_id}|{timestamp}"
    """
    # 1. Challenge freshness
    nonce = _consume_challenge(challenge_id)
    if nonce is None:
        _audit(event="handshake_fail", peer_node_id=peer_node_id,
               reason="challenge_stale_or_unknown", request=request)
        raise HTTPException(401, "Challenge expired or unknown")

    # 2. Timestamp sanity (within 60s of now)
    now = int(time.time())
    if abs(now - timestamp) > 60:
        _audit(event="handshake_fail", peer_node_id=peer_node_id,
               reason="timestamp_skew", request=request,
               metadata={"client_ts": timestamp, "server_ts": now})
        raise HTTPException(401, "Clock skew too large")

    # 3. Decode pubkey, verify signature
    try:
        pubkey_bytes = base64.b64decode(pubkey_b64)
        if len(pubkey_bytes) != 32:
            raise ValueError("Ed25519 pubkey must be 32 bytes")
    except Exception:
        _audit(event="handshake_fail", peer_node_id=peer_node_id,
               reason="pubkey_decode", request=request)
        raise HTTPException(400, "Invalid pubkey encoding")

    message = f"{challenge_id}|{peer_node_id}|{timestamp}".encode("utf-8")
    if not _verify_ed25519(pubkey_bytes, message, signature_b64):
        _audit(event="handshake_fail", peer_node_id=peer_node_id,
               reason="signature_invalid", request=request)
        raise HTTPException(401, "Signature verification failed")

    fingerprint = _pubkey_fingerprint(pubkey_bytes)

    # 4. Validate + consume invitation token, insert membership atomically
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT issued_by_node_id, expires_at, initial_roles, status
                  FROM peer_invitations
                 WHERE invitation_token = %s
                   FOR UPDATE
                """,
                (invitation_token,),
            )
            inv = cur.fetchone()
            if inv is None:
                _audit(event="invitation_rejected", peer_node_id=peer_node_id,
                       reason="token_unknown", request=request)
                raise HTTPException(401, "Invitation token unknown")
            issuer, expires_at, initial_roles, status = inv
            if status != "open":
                _audit(event="invitation_rejected", peer_node_id=peer_node_id,
                       reason=f"token_status_{status}", request=request)
                raise HTTPException(401, f"Invitation token is {status}")
            if expires_at < datetime.now(timezone.utc):
                cur.execute(
                    "UPDATE peer_invitations SET status='expired' WHERE invitation_token=%s",
                    (invitation_token,),
                )
                conn.commit()
                _audit(event="invitation_rejected", peer_node_id=peer_node_id,
                       reason="token_expired", request=request)
                raise HTTPException(401, "Invitation token expired")

            # Check no existing membership conflicts (same node_id or same pubkey)
            cur.execute(
                """
                SELECT peer_node_id FROM peer_memberships
                 WHERE peer_node_id = %s OR pubkey_fingerprint = %s
                """,
                (peer_node_id, fingerprint),
            )
            existing = cur.fetchone()
            if existing:
                _audit(event="handshake_fail", peer_node_id=peer_node_id,
                       reason="duplicate_membership", request=request,
                       metadata={"existing": existing[0]})
                raise HTTPException(409, "Peer node_id or pubkey already registered")

            # Insert membership
            cur.execute(
                """
                INSERT INTO peer_memberships (
                    peer_node_id, pubkey_ed25519, pubkey_fingerprint,
                    invited_by_node_id, invitation_token, invited_at,
                    activated_at, status, roles, metadata, last_seen_at
                ) VALUES (
                    %s, %s, %s, %s, %s, NOW(), NOW(), 'active', %s::jsonb,
                    %s::jsonb, NOW()
                )
                """,
                (peer_node_id, pubkey_bytes, fingerprint, issuer,
                 invitation_token, json.dumps(initial_roles),
                 json.dumps(metadata or {})),
            )

            # Mark invitation consumed
            ip = request.client.host if (request and request.client) else None
            cur.execute(
                """
                UPDATE peer_invitations
                   SET status='consumed', consumed_at=NOW(),
                       consumed_by_node_id=%s, consumed_by_ip=%s
                 WHERE invitation_token=%s
                """,
                (peer_node_id, ip, invitation_token),
            )

            # Roles for JWT
            roles = initial_roles if isinstance(initial_roles, list) else json.loads(initial_roles)
        conn.commit()

    _audit(event="invitation_consumed", peer_node_id=peer_node_id,
           reason="ok", request=request,
           metadata={"invitation_token_prefix": invitation_token[:8]})
    _audit(event="handshake_ok", peer_node_id=peer_node_id, request=request)

    token, exp = _issue_jwt(peer_node_id, roles, trust_score=0.5)
    _audit(event="jwt_issued", peer_node_id=peer_node_id, request=request,
           metadata={"exp": exp})

    return {
        "jwt": token,
        "expires_at": exp,
        "peer_node_id": peer_node_id,
        "roles": roles,
    }


# ── Handshake: refresh ────────────────────────────────────────────────────


def handshake_refresh(
    *,
    peer_node_id: str,
    challenge_id: str,
    signature_b64: str,
    timestamp: int,
    request: Optional[Request] = None,
) -> dict:
    """Existing peer renews JWT by signing a fresh challenge with the
    same Ed25519 key that was registered at invitation."""
    nonce = _consume_challenge(challenge_id)
    if nonce is None:
        _audit(event="handshake_fail", peer_node_id=peer_node_id,
               reason="challenge_stale_or_unknown", request=request)
        raise HTTPException(401, "Challenge expired or unknown")

    now = int(time.time())
    if abs(now - timestamp) > 60:
        _audit(event="handshake_fail", peer_node_id=peer_node_id,
               reason="timestamp_skew", request=request)
        raise HTTPException(401, "Clock skew too large")

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pubkey_ed25519, status, roles, trust_score
                  FROM peer_memberships
                 WHERE peer_node_id = %s
                """,
                (peer_node_id,),
            )
            row = cur.fetchone()
            if row is None:
                _audit(event="handshake_fail", peer_node_id=peer_node_id,
                       reason="unknown_peer", request=request)
                raise HTTPException(401, "Unknown peer")
            pubkey_bytes, status, roles, trust_score = row
            if status != "active":
                _audit(event="handshake_fail", peer_node_id=peer_node_id,
                       reason=f"peer_status_{status}", request=request)
                raise HTTPException(403, f"Peer is {status}")

            message = f"{challenge_id}|{peer_node_id}|{timestamp}".encode("utf-8")
            if not _verify_ed25519(bytes(pubkey_bytes), message, signature_b64):
                _audit(event="handshake_fail", peer_node_id=peer_node_id,
                       reason="signature_invalid", request=request)
                raise HTTPException(401, "Signature verification failed")

            cur.execute(
                "UPDATE peer_memberships SET last_seen_at=NOW() WHERE peer_node_id=%s",
                (peer_node_id,),
            )
        conn.commit()

    roles_list = roles if isinstance(roles, list) else json.loads(roles)
    token, exp = _issue_jwt(peer_node_id, roles_list, float(trust_score))
    _audit(event="jwt_refreshed", peer_node_id=peer_node_id, request=request,
           metadata={"exp": exp})
    return {
        "jwt": token,
        "expires_at": exp,
        "peer_node_id": peer_node_id,
        "roles": roles_list,
    }


# ── JWT issuance + verification ───────────────────────────────────────────


def _issue_jwt(peer_node_id: str, roles: list, trust_score: float) -> tuple[str, int]:
    """Sign a JWT with the hub's symmetric secret. Returns (token, exp_unix)."""
    now = int(time.time())
    exp = now + JWT_TTL_S
    payload = {
        "sub": peer_node_id,
        "typ": "peer",
        "roles": roles,
        "trust": round(trust_score, 3),
        "iat": now,
        "exp": exp,
    }
    token = jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALG)
    return token, exp


def verify_jwt(token: str) -> Optional[PeerAuthClaims]:
    """Validate a peer JWT. Returns claims or None.
    Does not check membership status — the JWT TTL bounds the window.

    Returns None silently when ENKI_PEER_JWT_SECRET is unset — that's a
    valid state for non-hub nodes, where every Bearer token belongs to
    a different auth path (local-JWT user session). Raising 503 here
    would block the whole auth chain instead of letting it fall through."""
    secret = os.environ.get("ENKI_PEER_JWT_SECRET", "").strip()
    if not secret:
        return None
    try:
        payload = jwt.decode(token, secret, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None
    if payload.get("typ") != "peer":
        return None
    roles = payload.get("roles") or []
    if not isinstance(roles, list):
        return None
    return PeerAuthClaims(
        peer_node_id=payload["sub"],
        roles=roles,
        trust_score=float(payload.get("trust", 0.5)),
        issued_at=int(payload["iat"]),
        expires_at=int(payload["exp"]),
    )


# ── Bootstrap (one-shot install flow) ─────────────────────────────────────


def bootstrap_admin(
    *,
    pubkey_b64: str,
    bootstrap_secret: str,
    request: Optional[Request] = None,
) -> dict:
    """One-shot endpoint for the install script to register the federation
    admin's pubkey. Guarded by a bootstrap_secret kept in /etc/enki/hub.env
    that the install script knows; once consumed, the admin row flips from
    pending to active and the secret is rotated to make this endpoint
    permanently inert."""
    expected = os.environ.get("ENKI_HUB_BOOTSTRAP_SECRET", "").strip()
    if not expected:
        raise HTTPException(503, "Bootstrap secret not configured on hub")
    if not secrets.compare_digest(bootstrap_secret, expected):
        _audit(event="handshake_fail", reason="bootstrap_secret_mismatch",
               request=request)
        raise HTTPException(401, "Invalid bootstrap secret")

    try:
        pubkey_bytes = base64.b64decode(pubkey_b64)
        if len(pubkey_bytes) != 32:
            raise ValueError
    except Exception:
        raise HTTPException(400, "Invalid pubkey encoding")
    fingerprint = _pubkey_fingerprint(pubkey_bytes)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM peer_memberships WHERE peer_node_id='bootstrap_admin'"
            )
            row = cur.fetchone()
            if row is None:
                raise HTTPException(500, "Bootstrap admin row missing — re-run migration 103")
            if row[0] == 'active':
                _audit(event="handshake_fail", reason="bootstrap_already_consumed",
                       request=request)
                raise HTTPException(409, "Bootstrap already completed")

            cur.execute(
                """
                UPDATE peer_memberships
                   SET pubkey_ed25519 = %s,
                       pubkey_fingerprint = %s,
                       status = 'active',
                       activated_at = NOW(),
                       last_seen_at = NOW(),
                       updated_at = NOW()
                 WHERE peer_node_id = 'bootstrap_admin'
                """,
                (pubkey_bytes, fingerprint),
            )
        conn.commit()

    _audit(event="invitation_consumed", peer_node_id="bootstrap_admin",
           reason="bootstrap_ok", request=request)
    log.warning("Bootstrap admin activated; rotate ENKI_HUB_BOOTSTRAP_SECRET to seal.")

    token, exp = _issue_jwt("bootstrap_admin", ["federation_admin"], 1.0)
    return {"jwt": token, "expires_at": exp, "peer_node_id": "bootstrap_admin",
            "roles": ["federation_admin"]}


# ── Invitation issuance (admin operation) ─────────────────────────────────


def issue_invitation(
    *,
    issued_by_node_id: str,
    initial_roles: Optional[list] = None,
    intended_for_label: Optional[str] = None,
    ttl_seconds: int = 7 * 24 * 3600,
    request: Optional[Request] = None,
) -> dict:
    """Federation admin mints a single-use onboarding token. Returned to
    the operator out-of-band (admin panel UI shows it once)."""
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    roles = initial_roles or ["peer"]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO peer_invitations (
                    invitation_token, issued_by_node_id, expires_at,
                    intended_for_label, initial_roles
                ) VALUES (%s, %s, %s, %s, %s::jsonb)
                """,
                (token, issued_by_node_id, expires_at,
                 intended_for_label, json.dumps(roles)),
            )
        conn.commit()
    _audit(event="invitation_issued", peer_node_id=issued_by_node_id,
           reason=intended_for_label, request=request,
           metadata={"token_prefix": token[:8], "roles": roles,
                     "expires_at": expires_at.isoformat()})
    return {"invitation_token": token, "expires_at": expires_at.isoformat(),
            "initial_roles": roles}


# ── Revocation ────────────────────────────────────────────────────────────


def revoke_peer(
    *,
    peer_node_id: str,
    revoked_by: str,
    reason: Optional[str] = None,
    request: Optional[Request] = None,
) -> dict:
    """Federation admin disables a peer. Existing JWTs continue working
    until their TTL expires (max JWT_TTL_S seconds); next refresh fails."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE peer_memberships SET status='revoked', updated_at=NOW()
                 WHERE peer_node_id=%s AND status != 'revoked'
             RETURNING peer_node_id
                """,
                (peer_node_id,),
            )
            row = cur.fetchone()
        conn.commit()
    if row is None:
        raise HTTPException(404, "Peer not found or already revoked")
    _audit(event="revoked", peer_node_id=peer_node_id, reason=reason,
           request=request, metadata={"revoked_by": revoked_by})
    return {"peer_node_id": peer_node_id, "status": "revoked"}
