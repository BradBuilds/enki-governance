# storage/auth/authenticate.py
"""
Authentication:
- Validates device grant (exists, signature valid, active, tenant scoped)
- Validates challenge (exists, bound to device_id, not expired, not consumed)
- Validates body hash
- Verifies signature over canonical envelope
- Consumes challenge on success
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric import ed25519, ec
from cryptography.hazmat.primitives import hashes

from storage.auth.canonical import canonical_json_bytes
from storage.auth.grant import DeviceGrant, load_device_public_key, is_grant_active, verify_device_grant_signature
from storage.auth.challenge_store import InMemoryChallengeStore


@dataclass
class AuthResult:
    success: bool
    device_id: Optional[str] = None
    device_tier: Optional[str] = None
    tenant_scopes: Optional[list[str]] = None
    reason: Optional[str] = None


def build_signature_envelope(
    *,
    device_id: str,
    challenge_id: str,
    tenant_id: str,
    method: str,
    path: str,
    body_sha256: str,
    nonce: str,
    issued_at: str,
    expires_at: str,
) -> bytes:
    """
    Canonical signing envelope — pipe-delimited format.

    Format: key1:val1|key2:val2|... (keys sorted alphabetically)

    This format is deterministic across all platforms (Android Kotlin,
    Python, JavaScript). No JSON escaping differences (forward slashes,
    unicode), no library-specific serialization quirks.

    MUST match the mobile DeviceSigner.buildEnvelope() and desktop
    DeviceSigner.build_envelope() exactly.
    """
    components = {
        "body_sha256": body_sha256,
        "challenge_id": challenge_id,
        "device_id": device_id,
        "expires_at": expires_at,
        "issued_at": issued_at,
        "method": method.upper(),
        "nonce": nonce,
        "path": path,
        "tenant_id": tenant_id,
    }
    envelope = "|".join(f"{k}:{components[k]}" for k in sorted(components))
    return envelope.encode("utf-8")


def authenticate_device(
    *,
    device_id: str,
    challenge_id: str,
    tenant_id: str,
    method: str,
    path: str,
    body: bytes,
    body_sha256_header: str,
    signature_b64: str,
    grant: Optional[DeviceGrant],
    root_public_key_raw: bytes,
    challenge_store: InMemoryChallengeStore,
) -> AuthResult:
    # 1) grant exists
    if not grant:
        return AuthResult(success=False, reason="AUTH.DEVICE_NOT_ENROLLED")

    # 2) grant signature valid
    if not verify_device_grant_signature(grant, root_public_key_raw):
        return AuthResult(success=False, reason="AUTH.GRANT_SIGNATURE_INVALID")

    # 3) grant active (expiry / revocation)
    active, reason = is_grant_active(grant)
    if not active:
        return AuthResult(success=False, reason=reason or "AUTH.GRANT_INACTIVE")

    # 4) tenant scope
    if tenant_id not in grant.tenant_scopes:
        return AuthResult(success=False, reason="TENANT.SCOPE_FORBIDDEN")

    # 5) challenge exists
    ch = challenge_store.get(challenge_id)
    if not ch:
        return AuthResult(success=False, reason="AUTH.CHALLENGE_NOT_FOUND")

    # 6) challenge valid + bound
    ok, ch_reason = challenge_store.is_valid(ch)
    if not ok:
        return AuthResult(success=False, reason=ch_reason or "AUTH.CHALLENGE_INVALID")

    if ch.device_id != device_id:
        return AuthResult(success=False, reason="AUTH.CHALLENGE_DEVICE_MISMATCH")

    # 7) body hash matches
    computed = hashlib.sha256(body).hexdigest()
    if computed != body_sha256_header:
        return AuthResult(success=False, reason="INGEST.BODY_HASH_MISMATCH")

    # 8) verify request signature
    msg = build_signature_envelope(
        device_id=device_id,
        challenge_id=challenge_id,
        tenant_id=tenant_id,
        method=method,
        path=path,
        body_sha256=computed,
        nonce=ch.nonce,
        issued_at=ch.issued_at.isoformat(),
        expires_at=ch.expires_at.isoformat(),
    )

    sig = base64.b64decode(signature_b64)
    pub = load_device_public_key(grant)

    try:
        if isinstance(pub, ed25519.Ed25519PublicKey):
            pub.verify(sig, msg)
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            # DER-encoded ECDSA signature expected
            pub.verify(sig, msg, ec.ECDSA(hashes.SHA256()))
        else:
            return AuthResult(success=False, reason="AUTH.UNSUPPORTED_ALGORITHM")
    except InvalidSignature:
        return AuthResult(success=False, reason="AUTH.SIGNATURE_INVALID")
    except Exception as e:
        return AuthResult(success=False, reason=f"AUTH.VERIFICATION_ERROR:{type(e).__name__}")

    # 9) consume challenge
    if not challenge_store.consume(challenge_id):
        return AuthResult(success=False, reason="AUTH.CHALLENGE_CONSUMPTION_FAILED")

    return AuthResult(
        success=True,
        device_id=device_id,
        device_tier=grant.device_tier,
        tenant_scopes=grant.tenant_scopes,
    )
