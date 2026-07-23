# storage/auth/grant.py
"""
DeviceGrant issuance + verification.

Tier hierarchy (issuer → issued):
  R (Root)        air-gapped HWKEY in a safe; signs Tier K grants only
  K (Master)      online HWKEY held by an operator; signs Tier P grants and
                    governance ops (pack admission, migrations, code updates)
  P (Privileged)  software grant signed by K; signs Tier W and routine writes
  W (Worker)      software grant signed by P (or K in dev); workers + sessions

Each grant carries the immediate issuer's signature. To verify a grant,
the verifier walks parent_grant_chain up to a Tier R pubkey listed in
trusted_keys.signed.

Algorithms: root and parent signatures are always Ed25519. Device keys
may be Ed25519 (preferred) or ECDSA P-256 (legacy).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional, Literal
import uuid

from cryptography.hazmat.primitives.asymmetric import ed25519, ec
from cryptography.hazmat.primitives import serialization

from storage.auth.canonical import canonical_json_bytes
from storage.auth.root_key import RootKey

DeviceTier = Literal["R", "K", "P", "W"]
Algorithm = Literal["Ed25519", "ECDSA_P256"]


# Allowed "issuer tier → issued tier" transitions. Anything else is rejected
# at issuance time AND at verification time.
ALLOWED_PARENT_TIER: dict[DeviceTier, set[DeviceTier]] = {
    "R": set(),          # Root has no parent (self-signed)
    "K": {"R"},          # K must be issued by R
    "P": {"K"},          # P must be issued by K
    "W": {"P", "K"},     # W issued by P; K can issue W directly in dev
}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class DeviceGrant:
    grant_id: str
    device_id: str

    # device pubkey
    public_key: str                 # base64
    public_key_algorithm: Algorithm

    # authorization semantics
    device_tier: DeviceTier
    tenant_scopes: list[str]

    issued_at: str                  # ISO8601
    expires_at: str                 # ISO8601 (required)

    # ── Parent grant (issuer chain) ───────────────────────────────────────
    # The pubkey of the grant that signed THIS one. None only for Tier R
    # (self-signed at genesis ceremony).
    parent_grant_id: Optional[str] = None
    parent_public_key: Optional[str] = None        # base64 of issuer pubkey
    parent_tier: Optional[DeviceTier] = None       # tier of the issuer

    # revocation
    revoked_at: Optional[str] = None
    revocation_reason: Optional[str] = None

    # signature over canonical payload by the parent (or self for Tier R)
    enki_signature: str = ""        # base64 (Ed25519)

    def to_dict(self) -> dict:
        return asdict(self)

    def canonical_payload(self) -> dict:
        """
        Canonical payload for parent (or root) signing.
        NOTE: exclude enki_signature itself. Order is alphabetical for
        deterministic JSON serialization.
        """
        return {
            "device_id": self.device_id,
            "device_tier": self.device_tier,
            "expires_at": self.expires_at,
            "grant_id": self.grant_id,
            "issued_at": self.issued_at,
            "parent_grant_id": self.parent_grant_id,
            "parent_public_key": self.parent_public_key,
            "parent_tier": self.parent_tier,
            "public_key": self.public_key,
            "public_key_algorithm": self.public_key_algorithm,
            "revoked_at": self.revoked_at,
            "revocation_reason": self.revocation_reason,
            "tenant_scopes": sorted(self.tenant_scopes),
        }

    def canonical_message(self) -> bytes:
        return canonical_json_bytes(self.canonical_payload())


def encode_public_key_b64(public_key_obj) -> tuple[str, Algorithm]:
    """
    Encode a device public key to base64 + algorithm label.
    - Ed25519: raw 32 bytes
    - ECDSA P-256: X9.62 uncompressed point
    """
    if isinstance(public_key_obj, ed25519.Ed25519PublicKey):
        b = public_key_obj.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return base64.b64encode(b).decode("ascii"), "Ed25519"

    if isinstance(public_key_obj, ec.EllipticCurvePublicKey):
        b = public_key_obj.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        return base64.b64encode(b).decode("ascii"), "ECDSA_P256"

    raise ValueError("Unsupported public key object type")


def load_device_public_key(grant: DeviceGrant):
    key_bytes = base64.b64decode(grant.public_key)

    if grant.public_key_algorithm == "Ed25519":
        return ed25519.Ed25519PublicKey.from_public_bytes(key_bytes)

    if grant.public_key_algorithm == "ECDSA_P256":
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), key_bytes)

    raise ValueError(f"Unsupported algorithm: {grant.public_key_algorithm}")


def issue_root_grant(
    *,
    device_id: str,
    public_key_bytes: bytes,
    root_key: RootKey,
    expires_at: str,
    tenant_scopes: list[str],
) -> DeviceGrant:
    """Issue the genesis Tier R grant — self-signed by the root key.

    The pubkey embedded in the grant must match root_key's pubkey exactly
    (this is what makes the grant self-signed). Tier R has no parent.
    """
    self_pub_b64 = base64.b64encode(public_key_bytes).decode("ascii")
    expected_pub_b64 = root_key.public_key_base64()
    if self_pub_b64 != expected_pub_b64:
        raise ValueError(
            "Root grant pubkey must match root_key's pubkey. "
            f"got={self_pub_b64[:16]}... expected={expected_pub_b64[:16]}..."
        )

    grant = DeviceGrant(
        grant_id=str(uuid.uuid4()),
        device_id=device_id,
        public_key=self_pub_b64,
        public_key_algorithm="Ed25519",
        device_tier="R",
        tenant_scopes=tenant_scopes,
        issued_at=utcnow_iso(),
        expires_at=expires_at,
        parent_grant_id=None,
        parent_public_key=None,
        parent_tier=None,
    )

    sig = root_key.sign(grant.canonical_message())
    grant.enki_signature = base64.b64encode(sig).decode("ascii")
    return grant


def issue_child_grant(
    *,
    device_id: str,
    public_key_bytes: bytes,
    public_key_algorithm: Algorithm,
    device_tier: DeviceTier,
    tenant_scopes: list[str],
    expires_at: str,
    parent_grant: DeviceGrant,
    parent_signer: RootKey,
) -> DeviceGrant:
    """Issue a child grant (K, P, or W) signed by the parent's keypair.

    parent_grant: the issuer's grant (provides parent_grant_id, parent pubkey,
                  parent_tier for the child's chain).
    parent_signer: a RootKey-shaped object (real RootKey or HardwareKeyAdapter)
                   holding the parent's PRIVATE key for signing.

    Validates the issuer→issued tier transition before signing.
    """
    if device_tier == "R":
        raise ValueError(
            "Tier R grants must be issued via issue_root_grant() (self-signed)."
        )
    parent_tier = parent_grant.device_tier
    allowed = ALLOWED_PARENT_TIER.get(device_tier, set())
    if parent_tier not in allowed:
        raise ValueError(
            f"Tier {parent_tier} cannot issue Tier {device_tier}. "
            f"Tier {device_tier} must be issued by one of: {sorted(allowed)}"
        )

    grant = DeviceGrant(
        grant_id=str(uuid.uuid4()),
        device_id=device_id,
        public_key=base64.b64encode(public_key_bytes).decode("ascii"),
        public_key_algorithm=public_key_algorithm,
        device_tier=device_tier,
        tenant_scopes=tenant_scopes,
        issued_at=utcnow_iso(),
        expires_at=expires_at,
        parent_grant_id=parent_grant.grant_id,
        parent_public_key=parent_grant.public_key,
        parent_tier=parent_grant.device_tier,
    )

    sig = parent_signer.sign(grant.canonical_message())
    grant.enki_signature = base64.b64encode(sig).decode("ascii")
    return grant


# ── Backward-compatible alias for tests that haven't moved over ────────────
# Existing call sites use `issue_device_grant` for the simple "root signs
# directly" case. We keep that signature but now it produces a grant whose
# parent_* fields point at the root pubkey, making it a real chain.
def issue_device_grant(
    *,
    device_id: str,
    public_key_bytes: bytes,
    public_key_algorithm: Algorithm,
    device_tier: DeviceTier,
    tenant_scopes: list[str],
    root_key: RootKey,
    expires_at: str,
) -> DeviceGrant:
    """Issue a grant signed directly by the root key.

    For Tier R, this is the self-signed genesis grant.
    For other tiers, the root acts as the immediate parent — this is the
    DEV / single-operator shortcut. In production with full hierarchy,
    use issue_child_grant() with a real parent grant.
    """
    if device_tier == "R":
        return issue_root_grant(
            device_id=device_id,
            public_key_bytes=public_key_bytes,
            root_key=root_key,
            expires_at=expires_at,
            tenant_scopes=tenant_scopes,
        )

    root_pub_b64 = root_key.public_key_base64()
    grant = DeviceGrant(
        grant_id=str(uuid.uuid4()),
        device_id=device_id,
        public_key=base64.b64encode(public_key_bytes).decode("ascii"),
        public_key_algorithm=public_key_algorithm,
        device_tier=device_tier,
        tenant_scopes=tenant_scopes,
        issued_at=utcnow_iso(),
        expires_at=expires_at,
        parent_grant_id=None,         # root is parent, not a grant
        parent_public_key=root_pub_b64,
        parent_tier="R",
    )

    sig = root_key.sign(grant.canonical_message())
    grant.enki_signature = base64.b64encode(sig).decode("ascii")
    return grant


def verify_device_grant_signature(grant: DeviceGrant, root_public_key_raw: bytes) -> bool:
    """Verify the grant's enki_signature against the configured signer.

    For grants with parent_public_key set, that pubkey is the signer.
    For grants without (legacy / direct-from-root), root_public_key_raw is.

    NOTE: This verifies only the immediate signature. To verify the full
    chain back to a Tier R pubkey listed in trusted_keys.signed, use
    verify_grant_chain().
    """
    msg = grant.canonical_message()
    sig = base64.b64decode(grant.enki_signature)

    if grant.parent_public_key:
        # Parent's pubkey signed it
        signer_bytes = base64.b64decode(grant.parent_public_key)
        pub = ed25519.Ed25519PublicKey.from_public_bytes(signer_bytes)
    else:
        # Direct from root
        pub = ed25519.Ed25519PublicKey.from_public_bytes(root_public_key_raw)

    try:
        pub.verify(sig, msg)
        return True
    except Exception:
        return False


def verify_grant_chain(
    grant: DeviceGrant,
    *,
    parent_lookup,  # callable: (parent_grant_id: str) -> Optional[DeviceGrant]
    trusted_root_pubkeys: set[str],  # base64 pubkeys of trusted Tier R keys
) -> tuple[bool, str]:
    """Walk parent_grant_id chain, verifying each signature, until we hit
    a self-signed Tier R grant whose pubkey is in trusted_root_pubkeys.

    Returns (ok, reason). reason is one of:
      "OK" — chain valid, terminates at a trusted Tier R
      "AUTH.GRANT_BAD_SIGNATURE" — a signature didn't verify
      "AUTH.GRANT_TIER_TRANSITION" — illegal tier transition in chain
      "AUTH.GRANT_BAD_CHAIN" — broken chain (missing parent)
      "AUTH.GRANT_UNTRUSTED_ROOT" — terminates at non-trusted Tier R
      "AUTH.GRANT_CHAIN_TOO_DEEP" — chain longer than 8 (sanity bound)
    """
    visited: set[str] = set()
    current = grant
    depth = 0

    while True:
        depth += 1
        if depth > 8:
            return False, "AUTH.GRANT_CHAIN_TOO_DEEP"

        # Verify this grant's signature
        if current.parent_public_key:
            signer_bytes = base64.b64decode(current.parent_public_key)
        elif current.device_tier == "R":
            # Self-signed root
            signer_bytes = base64.b64decode(current.public_key)
        else:
            return False, "AUTH.GRANT_BAD_CHAIN"

        msg = current.canonical_message()
        sig = base64.b64decode(current.enki_signature)
        try:
            ed25519.Ed25519PublicKey.from_public_bytes(signer_bytes).verify(sig, msg)
        except Exception:
            return False, "AUTH.GRANT_BAD_SIGNATURE"

        # Tier transition validity (only meaningful for non-R)
        if current.device_tier != "R":
            allowed = ALLOWED_PARENT_TIER.get(current.device_tier, set())
            if current.parent_tier not in allowed:
                return False, "AUTH.GRANT_TIER_TRANSITION"

        # If this is Tier R, terminate
        if current.device_tier == "R":
            if current.public_key in trusted_root_pubkeys:
                return True, "OK"
            return False, "AUTH.GRANT_UNTRUSTED_ROOT"

        # Walk up
        if current.parent_grant_id is None:
            # No parent grant id — must be a direct-from-root grant.
            # Trust if parent_public_key is in trusted_root_pubkeys.
            if current.parent_public_key in trusted_root_pubkeys:
                return True, "OK"
            return False, "AUTH.GRANT_UNTRUSTED_ROOT"

        if current.parent_grant_id in visited:
            return False, "AUTH.GRANT_BAD_CHAIN"  # cycle
        visited.add(current.parent_grant_id)

        parent = parent_lookup(current.parent_grant_id)
        if parent is None:
            return False, "AUTH.GRANT_BAD_CHAIN"
        current = parent


def is_grant_active(grant: DeviceGrant) -> tuple[bool, str | None]:
    """
    Returns (active, reason_code).
    Reason codes are stable strings suitable for Spec-aligned error mapping.
    """
    if grant.revoked_at is not None:
        return False, "AUTH.GRANT_REVOKED"

    try:
        exp = datetime.fromisoformat(grant.expires_at)
    except Exception:
        return False, "AUTH.GRANT_EXPIRES_AT_INVALID"

    if datetime.now(timezone.utc) >= exp:
        return False, "AUTH.GRANT_EXPIRED"

    return True, None
