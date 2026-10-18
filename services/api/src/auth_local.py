"""
Local Authenticator-App Authentication — for hardware / node-local nodes.

Authentication is QR-only: the authenticator app (Google Authenticator,
1Password, Bitwarden, Authy, anything RFC 6238-compatible) IS the
credential. No password. Backup codes are the only recovery path.

Why no password: a password adds an attack surface (phishing, reuse,
brute force) without adding security beyond what TOTP already gives.
For a single-admin signal-intelligence node, possession of the
authenticator app is the security boundary; that's the same boundary
password+TOTP nets you, with less to manage.

Flows:
  Signup (first user only):
    POST /auth/local/signup
        body: {email, display_name, tenant_name?}
        → creates identity + tenant in pending-verification state
        → generates TOTP secret + 12 single-use backup codes
        → returns {signup_token, otpauth_url, secret, backup_codes}
    POST /auth/local/signup/verify
        body: {signup_token, code}
        → verifies the 6-digit code against the secret
        → flips totp_enabled = TRUE, issues JWT

  Login:
    POST /auth/local/login
        body: {email, code, is_backup_code?}
        → verifies code (TOTP or backup) → issues JWT directly
        → no two-step nonce; possession of the app IS the credential

Hardware (HWKEY) nodes layer device-key headers on top — see unified_auth.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import bcrypt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

log = logging.getLogger("enki.auth.local")
router = APIRouter(prefix="/auth/local", tags=["auth"])

# JWT signing — uses HMAC-SHA256 with the node's secret key
# In production hardware mode, this could use Ed25519 from the node key
JWT_SECRET = os.environ.get("ENKI_SECRET_KEY", "")
JWT_ISSUER = "enki-local"
JWT_EXPIRY_HOURS = 48  # local sessions last longer (no refresh token)


# ─── Request Models ──────────────────────────────────────────────────────────

class SignupRequest(BaseModel):
    email: str
    display_name: str
    tenant_name: Optional[str] = None


class SignupVerifyRequest(BaseModel):
    signup_token: str
    code: str = Field(min_length=6, max_length=6)


class LoginRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=10)  # 6 for TOTP, 8 for backup
    is_backup_code: bool = False


# ─── Password Hashing ────────────────────────────────────────────────────────

def _hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))


# ─── JWT (HMAC-SHA256, self-issued) ───────────────────────────────────────────

def _issue_jwt(identity_id: str, tenant_id: str, email: str, role: str) -> str:
    """Issue a locally-signed JWT. No external auth server needed."""
    import base64

    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    payload = {
        "sub": identity_id,
        "tenant_id": tenant_id,
        "email": email,
        "role": role,
        "iss": JWT_ISSUER,
        "iat": now,
        "exp": now + (JWT_EXPIRY_HOURS * 3600),
    }

    def _b64(data: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

    header_b64 = _b64(header)
    payload_b64 = _b64(payload)
    signing_input = f"{header_b64}.{payload_b64}"

    signature = hmac.new(
        JWT_SECRET.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{header_b64}.{payload_b64}.{sig_b64}"


def validate_local_jwt(token: str) -> Optional[dict]:
    """Validate a locally-issued JWT. Returns claims or None."""
    import base64

    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, sig_b64 = parts

        # Verify signature
        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(
            JWT_SECRET.encode("utf-8"),
            signing_input.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        # Pad base64
        sig_b64_padded = sig_b64 + "=" * (4 - len(sig_b64) % 4)
        actual_sig = base64.urlsafe_b64decode(sig_b64_padded)

        if not hmac.compare_digest(expected_sig, actual_sig):
            return None

        # Decode payload
        payload_padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_padded))

        # Check expiry
        if payload.get("exp", 0) < time.time():
            return None

        # Check issuer
        if payload.get("iss") != JWT_ISSUER:
            return None

        return payload
    except Exception:
        return None


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.get("/status")
async def auth_status():
    """Public endpoint: returns whether any users exist (for login vs signup UI).

    A user "exists" only once they've completed TOTP enrollment — half-finished
    signups (identity row created but totp_enabled=FALSE) don't count, so a
    closed browser tab mid-signup doesn't lock out the next attempt at the
    first-boot signup gate.
    """
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM identities WHERE COALESCE(totp_enabled, FALSE)")
                count = cur.fetchone()[0]
    except Exception:
        count = 0

    return {"has_users": count > 0, "user_count": count}


@router.post("/signup")
async def signup(body: SignupRequest):
    """Step 1 of first-boot signup. Creates the admin identity + tenant in
    pending-verification state and provisions a TOTP secret + 12 backup
    codes. The client renders the otpauth_url as a QR code; the user
    scans with their authenticator app and submits the first 6-digit
    code via /signup/verify.

    Idempotency: if a half-finished signup exists (identity present,
    totp_enabled=FALSE), we wipe it and start fresh — a closed-tab
    abandonment must not lock the first-boot gate forever.

    Only works while no fully-enrolled user exists. Subsequent users
    join via invite (separate flow, not yet exposed via UI).
    """
    from storage.postgres.db import get_connection
    import urllib.parse as _up
    import secrets as _secrets

    email = body.email.strip().lower()
    display_name = (body.display_name or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    if not display_name:
        raise HTTPException(status_code=400, detail="Display name required")

    issuer = "Enki"
    secret = _gen_totp_secret()

    # 12 backup codes, 8 chars from a no-ambiguous-chars alphabet
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, O, 0, 1
    backup_codes = ["".join(_secrets.choice(alphabet) for _ in range(8)) for _ in range(12)]
    backup_hashes = [(c, _hash_password(c)) for c in backup_codes]

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Reject if any user is fully enrolled
                cur.execute("SELECT COUNT(*) FROM identities WHERE COALESCE(totp_enabled, FALSE)")
                if cur.fetchone()[0] > 0:
                    raise HTTPException(
                        status_code=403,
                        detail="A user already exists on this node. Use an invite to add more.",
                    )

                # Wipe any half-finished prior attempt (totp_enabled=FALSE)
                cur.execute(
                    "SELECT identity_id FROM identities "
                    " WHERE NOT COALESCE(totp_enabled, FALSE)"
                )
                stale_ids = [r[0] for r in cur.fetchall()]
                if stale_ids:
                    cur.execute(
                        "DELETE FROM totp_backup_codes WHERE identity_id = ANY(%s)",
                        (stale_ids,),
                    )
                    cur.execute(
                        "DELETE FROM identities WHERE identity_id = ANY(%s)",
                        (stale_ids,),
                    )

                tenant_id = "default"
                tenant_name = body.tenant_name or f"{display_name}'s Station"

                try:
                    from storage.node.identity import load_node_id
                    node_id = load_node_id()
                except Exception:
                    node_id = "local"

                cur.execute("""
                    INSERT INTO tenants (tenant_id, node_id, display_name, status, created_at)
                    VALUES (%s, %s, %s, 'active', NOW())
                    ON CONFLICT (tenant_id) DO NOTHING
                """, (tenant_id, node_id, tenant_name))

                identity_id = f"person_{uuid.uuid4().hex[:12]}"
                cur.execute("""
                    INSERT INTO identities
                        (identity_id, tenant_id, display_name, role, email,
                         totp_secret, totp_enabled, metadata,
                         created_at, updated_at)
                    VALUES (%s, %s, %s, 'admin', %s, %s, FALSE, %s, NOW(), NOW())
                """, (
                    identity_id, tenant_id, display_name, email, secret,
                    json.dumps({"auth_method": "totp", "first_user": True}),
                ))

                for idx, (_, hashed) in enumerate(backup_hashes, start=1):
                    cur.execute(
                        "INSERT INTO totp_backup_codes (identity_id, code_hash, code_index) "
                        " VALUES (%s, %s, %s)",
                        (identity_id, hashed, idx),
                    )

                cur.execute("""
                    INSERT INTO user_preferences
                        (identity_id, tenant_id, role, default_view)
                    VALUES (%s, %s, %s, 'dashboard')
                    ON CONFLICT (identity_id) DO NOTHING
                """, (identity_id, tenant_id, 'admin'))

            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        log.error("Signup failed: %s", e)
        raise HTTPException(status_code=500, detail="Account creation failed")

    otpauth_url = (
        f"otpauth://totp/{_up.quote(issuer)}:{_up.quote(email)}"
        f"?secret={secret}&issuer={_up.quote(issuer)}&algorithm=SHA1"
        f"&digits=6&period=30"
    )

    signup_token = _issue_pending_token(
        identity_id=identity_id, tenant_id=tenant_id, email=email,
        role="admin", typ="signup_pending",
    )

    log.info("Signup step 1 (QR shown): %s (%s)", display_name, email)
    return {
        "signup_token":  signup_token,
        "ttl_seconds":   LOGIN_TOKEN_TTL_S,
        "otpauth_url":   otpauth_url,    # render as QR
        "secret":        secret,         # for manual entry under the QR
        "backup_codes":  backup_codes,   # SHOWN ONCE
        "issuer":        issuer,
        "account":       email,
        "identity_id":   identity_id,
    }


@router.post("/signup/verify")
async def signup_verify(body: SignupVerifyRequest, request: Request):
    """Step 2 of signup. Verifies the 6-digit code against the secret
    minted in /signup. On success flips totp_enabled=TRUE and issues
    the full JWT. On failure leaves the pending identity intact so the
    user can retry with a fresh code."""
    from storage.postgres.db import get_connection

    claims = _validate_pending_token(body.signup_token, expected_typ="signup_pending")
    if not claims:
        raise HTTPException(401, "Signup token expired or invalid — start signup over")

    identity_id = claims["sub"]
    tenant_id   = claims["tenant_id"]
    email       = claims["email"]
    role        = claims["role"]
    request_ip  = (request.client.host if request.client else None)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT totp_secret, COALESCE(totp_enabled, FALSE), display_name "
                    " FROM identities WHERE identity_id = %s",
                    (identity_id,),
                )
                row = cur.fetchone()
                if not row or not row[0]:
                    raise HTTPException(400, "Signup state lost — start signup over")
                secret, already_enabled, display_name = row
                if already_enabled:
                    # Idempotent: re-verifying an already-enabled account just logs them in
                    pass
                elif not _verify_totp(secret, body.code):
                    _audit_local_auth(event="signup_fail", identity_id=identity_id,
                                        email=email, request_ip=request_ip,
                                        reason="bad_code_at_signup")
                    raise HTTPException(400, "Invalid code — check your authenticator app's clock")
                else:
                    cur.execute(
                        "UPDATE identities SET totp_enabled = TRUE, "
                        "       totp_enrolled_at = NOW(), updated_at = NOW() "
                        " WHERE identity_id = %s",
                        (identity_id,),
                    )
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        log.error("Signup verify failed: %s", e)
        raise HTTPException(status_code=503, detail="Verification unavailable")

    token = _issue_jwt(identity_id, tenant_id, email, role)
    _audit_local_auth(event="signup_success", identity_id=identity_id,
                        email=email, request_ip=request_ip)
    log.info("Signup complete: %s (%s) as %s", display_name, email, role)
    return {
        "access_token": token,
        "token_type":   "bearer",
        "identity_id":  identity_id,
        "tenant_id":    tenant_id,
        "role":         role,
        "display_name": display_name,
        "first_user":   True,
    }


@router.post("/login")
async def login(body: LoginRequest, request: Request):
    """Email + 6-digit TOTP code (or 8-char backup code) → JWT.

    Single-step: possession of the authenticator app IS the credential, so
    there's no password phase. We deliberately return the same error message
    for "no such email" and "bad code" so anonymous probes can't enumerate
    valid emails."""
    from storage.postgres.db import get_connection

    email = body.email.strip().lower()
    request_ip = (request.client.host if request and request.client else None)
    generic_error = HTTPException(status_code=401, detail="Invalid email or code")

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT identity_id, tenant_id, display_name, role,
                           totp_secret, COALESCE(totp_enabled, FALSE)
                      FROM identities
                     WHERE email = %s AND COALESCE(totp_enabled, FALSE) = TRUE
                     LIMIT 1
                """, (email,))
                row = cur.fetchone()

                if not row:
                    _audit_local_auth(event="login_fail_totp", email=email,
                                        request_ip=request_ip, reason="no_such_email")
                    raise generic_error

                identity_id, tenant_id, display_name, role, secret, _enabled = row

                if body.is_backup_code:
                    cur.execute(
                        "SELECT code_id, code_hash FROM totp_backup_codes "
                        " WHERE identity_id = %s AND consumed_at IS NULL",
                        (identity_id,),
                    )
                    matched_id = None
                    for code_id, code_hash in cur.fetchall():
                        if _verify_password(body.code, code_hash):
                            matched_id = code_id
                            break
                    if matched_id is None:
                        _audit_local_auth(event="login_fail_totp", email=email,
                                            identity_id=identity_id, request_ip=request_ip,
                                            reason="bad_backup_code")
                        raise generic_error
                    cur.execute(
                        "UPDATE totp_backup_codes SET consumed_at = NOW(), consumed_ip = %s "
                        " WHERE code_id = %s AND consumed_at IS NULL",
                        (request_ip, matched_id),
                    )
                    if cur.rowcount == 0:
                        # Lost the race — another tab consumed it. Treat as already-used.
                        raise generic_error
                    _audit_local_auth(event="backup_code_used",
                                        identity_id=identity_id, email=email,
                                        request_ip=request_ip)
                else:
                    if not _verify_totp(secret, body.code):
                        _audit_local_auth(event="login_fail_totp", email=email,
                                            identity_id=identity_id, request_ip=request_ip,
                                            reason="bad_totp_code")
                        raise generic_error
            conn.commit()
    except HTTPException:
        raise
    except Exception as e:
        log.error("Login failed: %s", e)
        raise HTTPException(status_code=503, detail="Authentication unavailable")

    token = _issue_jwt(identity_id, tenant_id, email, role)
    _audit_local_auth(event="login_success", email=email,
                        identity_id=identity_id, request_ip=request_ip,
                        metadata={"mfa": "backup" if body.is_backup_code else "totp"})
    log.info("Login: %s (%s) as %s", display_name, email, role)

    return {
        "access_token": token,
        "token_type": "bearer",
        "identity_id": identity_id,
        "tenant_id": tenant_id,
        "role": role,
        "display_name": display_name,
    }


# ── TOTP (RFC 6238 — Time-based One-Time Password) ──────────────────────────

LOGIN_TOKEN_TTL_S = 300  # 5 min between password and TOTP step


def _gen_totp_secret() -> str:
    """Random 160-bit secret, base32-encoded (the standard QR format)."""
    import base64, secrets
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _verify_totp(secret_b32: str, code: str, window: int = 1) -> bool:
    """RFC 6238 TOTP verification. window=1 means accept the current 30-sec
    period plus ±1 period (3 valid codes total) to handle clock drift."""
    import base64, hmac as _hmac, struct
    code = (code or "").strip().replace(" ", "")
    if not code or not code.isdigit() or len(code) != 6:
        return False

    # Pad secret to 8-char boundary for base32 decode
    pad = (8 - len(secret_b32) % 8) % 8
    key = base64.b32decode(secret_b32 + "=" * pad, casefold=True)

    now_period = int(time.time()) // 30
    for offset in range(-window, window + 1):
        t = struct.pack(">Q", now_period + offset)
        h = _hmac.new(key, t, hashlib.sha1).digest()
        o = h[-1] & 0x0F
        truncated = ((h[o]   & 0x7F) << 24 |
                     (h[o+1] & 0xFF) << 16 |
                     (h[o+2] & 0xFF) <<  8 |
                     (h[o+3] & 0xFF))
        expected = f"{truncated % 1_000_000:06d}"
        if _hmac.compare_digest(expected, code):
            return True
    return False


def _issue_pending_token(*, identity_id: str, tenant_id: str, email: str,
                         role: str, typ: str) -> str:
    """Short-lived signed nonce used to bridge multi-step flows (signup → verify).
    Same HMAC-JWT format as the real JWT but with a non-session `typ` so the
    full JWT validator will refuse it. `typ` is e.g. 'signup_pending'."""
    import base64
    now = int(time.time())
    payload = {
        "sub":       identity_id,
        "tenant_id": tenant_id,
        "email":     email,
        "role":      role,
        "typ":       typ,
        "iat":       now,
        "exp":       now + LOGIN_TOKEN_TTL_S,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    def _b64(d):
        return base64.urlsafe_b64encode(
            json.dumps(d, separators=(",", ":")).encode("utf-8")
        ).rstrip(b"=").decode("ascii")
    secret = (os.environ.get("ENKI_SECRET_KEY") or "dev-secret-change-me").encode("utf-8")
    signing_input = f"{_b64(header)}.{_b64(payload)}".encode("ascii")
    sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).rstrip(b"=").decode("ascii")
    return f"{signing_input.decode('ascii')}.{sig_b64}"


def _validate_pending_token(token: str, *, expected_typ: str) -> Optional[dict]:
    """Verify a pending-flow token. Returns claims or None if invalid,
    expired, or `typ` doesn't match `expected_typ`."""
    import base64
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        secret = (os.environ.get("ENKI_SECRET_KEY") or "dev-secret-change-me").encode("utf-8")
        signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
        expected_sig = hmac.new(secret, signing_input, hashlib.sha256).digest()
        provided_sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        if not hmac.compare_digest(expected_sig, provided_sig):
            return None
        pad = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
        if payload.get("typ") != expected_typ:
            return None
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


class TotpSetupRequest(BaseModel):
    """Authenticated user requesting TOTP enrollment."""


class TotpVerifySetupRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


@router.post("/totp/setup")
async def totp_setup(request: Request):
    """Generate a new TOTP secret for the authenticated user, return the
    otpauth:// URL the UI renders as a QR code, plus 12 single-use
    backup codes. Caller MUST follow up with /totp/verify-setup to flip
    totp_enabled=true. Until then, login still works without a code."""
    from storage.postgres.db import get_connection
    import urllib.parse as _up

    # Require the caller to be logged in (cookie or bearer)
    token = request.cookies.get("enki_session")
    if not token:
        auth_h = request.headers.get("authorization", "")
        if auth_h.lower().startswith("bearer "):
            token = auth_h.split(" ", 1)[1]
    claims = validate_local_jwt(token) if token else None
    if not claims:
        raise HTTPException(401, "Login required to set up TOTP")

    identity_id = claims["sub"]
    email = claims.get("email") or "user"
    issuer = "Enki"

    secret = _gen_totp_secret()
    # otpauth URL — TOTP apps render this from a QR code
    otpauth_url = (
        f"otpauth://totp/{_up.quote(issuer)}:{_up.quote(email)}"
        f"?secret={secret}&issuer={_up.quote(issuer)}&algorithm=SHA1"
        f"&digits=6&period=30"
    )

    # Generate 12 backup codes (8 chars each, base32 minus ambiguous chars)
    import secrets as _secrets
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I, O, 0, 1
    backup_codes = [
        "".join(_secrets.choice(alphabet) for _ in range(8))
        for _ in range(12)
    ]
    backup_hashes = [(c, _hash_password(c)) for c in backup_codes]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE identities SET totp_secret = %s, totp_enabled = FALSE "
                " WHERE identity_id = %s",
                (secret, identity_id),
            )
            # Wipe any prior backup codes
            cur.execute("DELETE FROM totp_backup_codes WHERE identity_id = %s",
                        (identity_id,))
            for idx, (_, hashed) in enumerate(backup_hashes, start=1):
                cur.execute(
                    """INSERT INTO totp_backup_codes (identity_id, code_hash, code_index)
                         VALUES (%s, %s, %s)""",
                    (identity_id, hashed, idx),
                )
        conn.commit()

    return {
        "secret":      secret,        # show on UI under QR for manual entry
        "otpauth_url": otpauth_url,   # render as QR code
        "backup_codes": backup_codes,  # SHOWN ONCE — user must write down
        "issuer":      issuer,
        "account":     email,
    }


@router.post("/totp/verify-setup")
async def totp_verify_setup(body: TotpVerifySetupRequest, request: Request):
    """Confirm enrollment by entering the first 6-digit code from the
    authenticator app. Flips totp_enabled=true on success."""
    from storage.postgres.db import get_connection

    token = request.cookies.get("enki_session")
    if not token:
        auth_h = request.headers.get("authorization", "")
        if auth_h.lower().startswith("bearer "):
            token = auth_h.split(" ", 1)[1]
    claims = validate_local_jwt(token) if token else None
    if not claims:
        raise HTTPException(401, "Login required")

    identity_id = claims["sub"]

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT totp_secret, COALESCE(totp_enabled, FALSE) FROM identities WHERE identity_id = %s",
                (identity_id,),
            )
            row = cur.fetchone()
            if not row or not row[0]:
                raise HTTPException(400, "No TOTP enrollment in progress — call /totp/setup first")
            secret, already_enabled = row[0], row[1]
            if already_enabled:
                return {"ok": True, "already_enabled": True}

            if not _verify_totp(secret, body.code):
                _audit_local_auth(event="totp_verify_fail",
                                    identity_id=identity_id,
                                    request_ip=(request.client.host if request.client else None),
                                    reason="bad_code_at_setup")
                raise HTTPException(400, "Invalid code — check your authenticator app's clock")

            cur.execute(
                "UPDATE identities SET totp_enabled = TRUE, totp_enrolled_at = NOW() "
                " WHERE identity_id = %s",
                (identity_id,),
            )
        conn.commit()

    _audit_local_auth(event="totp_enrolled", identity_id=identity_id,
                        request_ip=(request.client.host if request.client else None))
    return {"ok": True, "totp_enabled": True}


def _audit_local_auth(*, event: str, identity_id: Optional[str] = None,
                      email: Optional[str] = None, reason: Optional[str] = None,
                      request_ip: Optional[str] = None, user_agent: Optional[str] = None,
                      metadata: Optional[dict] = None) -> None:
    """Best-effort append to local_auth_audit. Never raises."""
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO local_auth_audit
                         (identity_id, email, event, reason, request_ip,
                          user_agent, metadata)
                       VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)""",
                    (identity_id, email, event, reason, request_ip,
                     user_agent, json.dumps(metadata) if metadata else None),
                )
            conn.commit()
    except Exception as e:
        log.warning("audit write failed for event=%s: %s", event, e)


@router.get("/me")
async def get_me(request: Request):
    """Get current user info from JWT."""
    # Read token from cookie or Authorization header
    token = request.cookies.get("enki_session")
    if not token:
        auth_header = request.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

    if not token:
        return {"authenticated": False}

    claims = validate_local_jwt(token)
    if not claims:
        return {"authenticated": False, "reason": "invalid or expired token"}

    return {
        "authenticated": True,
        "identity_id": claims.get("sub"),
        "tenant_id": claims.get("tenant_id"),
        "email": claims.get("email"),
        "role": claims.get("role"),
    }
