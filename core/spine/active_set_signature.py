"""
Active Set signature: build, parse, and verify the Tier-K signature attached
to an active_set JSON file.

The signature commits to (a) the exact file bytes of the active_set and
(b) the validation transcript hash from the admission ceremony, so a
signature is invalidated if either the active_set or the ceremony differs.

Written by:
    scripts/governance/sign_active_set.py

Read by:
    core/profile/validator.py:_check_signed_active_set (boot-time gate)
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives.asymmetric import ed25519

# storage.auth.grant is imported lazily inside the verifier and grant-lookup
# helpers, NOT at module import time, so the host-side signer can use this
# module without pulling in the full storage/__init__.py dep chain
# (jsonschema, psycopg2, etc.).


# ── Domain-separated canonical message ─────────────────────────────────────
# Versioned so we can evolve the scheme without ambiguity. The leading domain
# string makes the message useless for any other signing context.

CANONICAL_DOMAIN = b"ENKI-ACTIVE-SET-SIG-v1"


def build_canonical_message(active_set_sha256: str, transcript_hash: str) -> bytes:
    """Bytes that the Tier-K key signs.

    Format (v1):
        ENKI-ACTIVE-SET-SIG-v1
        active_set_sha256=<sha256:hex>
        transcript_hash=<sha256:hex>
        <empty line>

    The trailing newline is part of the message. Both hashes MUST be the
    "sha256:<64 hex>" string form already used elsewhere in the spine.
    """
    if not active_set_sha256.startswith("sha256:"):
        raise ValueError("active_set_sha256 must be in 'sha256:<hex>' form")
    if not transcript_hash.startswith("sha256:"):
        raise ValueError("transcript_hash must be in 'sha256:<hex>' form")

    return (
        CANONICAL_DOMAIN + b"\n"
        + b"active_set_sha256=" + active_set_sha256.encode("ascii") + b"\n"
        + b"transcript_hash=" + transcript_hash.encode("ascii") + b"\n"
    )


def compute_active_set_file_hash(path: Path) -> str:
    """SHA-256 of the active_set file bytes, in 'sha256:<hex>' form.

    Hashes raw bytes (not normalized JSON) so the signature commits to the
    file exactly as written. Any whitespace change invalidates the sig.
    """
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return f"sha256:{h.hexdigest()}"


# ── Signed-artifact dataclass ──────────────────────────────────────────────

@dataclass(frozen=True)
class SignedActiveSet:
    """In-memory representation of <active_set>.signed.json."""
    version: str
    active_set_filename: str
    active_set_sha256: str
    transcript_hash: str
    signed_at: str
    signer_device_id: str
    signer_device_tier: str
    signer_public_key: str          # base64
    signer_grant_id: str
    signature: str                  # base64 Ed25519

    @classmethod
    def from_dict(cls, data: dict) -> "SignedActiveSet":
        signer = data["signer"]
        return cls(
            version=data["version"],
            active_set_filename=data["active_set_filename"],
            active_set_sha256=data["active_set_sha256"],
            transcript_hash=data["transcript_hash"],
            signed_at=data["signed_at"],
            signer_device_id=signer["device_id"],
            signer_device_tier=signer["device_tier"],
            signer_public_key=signer["public_key"],
            signer_grant_id=signer["grant_id"],
            signature=data["signature"],
        )

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "active_set_filename": self.active_set_filename,
            "active_set_sha256": self.active_set_sha256,
            "transcript_hash": self.transcript_hash,
            "signed_at": self.signed_at,
            "signer": {
                "device_id": self.signer_device_id,
                "device_tier": self.signer_device_tier,
                "public_key": self.signer_public_key,
                "grant_id": self.signer_grant_id,
            },
            "signature": self.signature,
        }


def build_signed_artifact(
    *,
    active_set_path: Path,
    transcript_hash: str,
    signer_grant: Any,  # any object exposing .device_id, .device_tier, .public_key, .grant_id
    signature_b64: str,
) -> SignedActiveSet:
    """Construct a SignedActiveSet from the inputs the signer has.

    `signer_grant` is duck-typed so this function can be called from the
    host-side signer (no storage.auth import) by passing a small dataclass
    or SimpleNamespace, OR from the container with a real DeviceGrant.
    """
    return SignedActiveSet(
        version="1.0.0",
        active_set_filename=Path(active_set_path).name,
        active_set_sha256=compute_active_set_file_hash(active_set_path),
        transcript_hash=transcript_hash,
        signed_at=datetime.now(timezone.utc).isoformat(),
        signer_device_id=signer_grant.device_id,
        signer_device_tier=signer_grant.device_tier,
        signer_public_key=signer_grant.public_key,
        signer_grant_id=signer_grant.grant_id,
        signature=signature_b64,
    )


def signed_artifact_path(active_set_path: Path) -> Path:
    """Sibling path: active_set_v6.json -> active_set_v6.signed.json."""
    p = Path(active_set_path)
    return p.with_suffix(".signed.json") if p.suffix == ".json" else p.parent / (p.name + ".signed.json")


def load_signed_active_set(path: Path) -> SignedActiveSet:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return SignedActiveSet.from_dict(data)


def save_signed_active_set(signed: SignedActiveSet, path: Path) -> None:
    Path(path).write_text(
        json.dumps(signed.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


# ── Trusted-keys file ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class TrustedRootKey:
    device_id: str
    tier: str
    public_key: str  # base64
    grant_id: Optional[str]
    comment: str


def load_trusted_root_pubkeys(path: Path) -> set[str]:
    """Return the set of base64 Tier-R pubkeys from /etc/enki/trusted_keys.signed."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: set[str] = set()
    for entry in data.get("trusted_root_pubkeys", []):
        if entry.get("tier") == "R" and entry.get("public_key"):
            out.add(entry["public_key"])
    return out


# ── Verification ───────────────────────────────────────────────────────────

# Failure codes (stable strings for boot logs and tests)
SIG_OK = "OK"
SIG_FILE_MISSING = "SIG.SIGNED_FILE_MISSING"
SIG_PARSE_ERROR = "SIG.SIGNED_FILE_PARSE_ERROR"
SIG_HASH_MISMATCH = "SIG.ACTIVE_SET_HASH_MISMATCH"
SIG_BAD_SIGNATURE = "SIG.BAD_SIGNATURE"
SIG_WRONG_TIER = "SIG.WRONG_TIER"
SIG_GRANT_MISSING = "SIG.SIGNER_GRANT_MISSING"
SIG_PUBKEY_MISMATCH = "SIG.SIGNER_PUBKEY_MISMATCH"
SIG_TRUSTED_KEYS_MISSING = "SIG.TRUSTED_KEYS_MISSING"
SIG_CHAIN_INVALID = "SIG.CHAIN_INVALID"


def verify_active_set_signature(
    *,
    active_set_path: Path,
    signed_path: Path,
    trusted_keys_path: Path,
    grant_lookup: Callable[[str], Optional[Any]],
) -> tuple[bool, str, dict]:
    """Verify the .signed.json against the active_set and trust root.

    Returns (ok, code, context). On success: (True, "OK", {...}).
    On failure the caller should refuse boot and surface `code`.

    grant_lookup: callable that maps grant_id -> DeviceGrant (or None).
                  Used to walk the issuer chain back to a Tier-R pubkey.
    """
    # 1. Signed file exists and parses
    signed_p = Path(signed_path)
    if not signed_p.exists():
        return False, SIG_FILE_MISSING, {"path": str(signed_p)}

    try:
        signed = load_signed_active_set(signed_p)
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        return False, SIG_PARSE_ERROR, {"path": str(signed_p), "error": str(e)}

    # 2. Active_set bytes match the hash committed to in the signature
    actual_hash = compute_active_set_file_hash(active_set_path)
    if actual_hash != signed.active_set_sha256:
        return False, SIG_HASH_MISMATCH, {
            "expected": signed.active_set_sha256,
            "actual": actual_hash,
        }

    # 3. Signature verifies against signer pubkey + canonical message
    msg = build_canonical_message(signed.active_set_sha256, signed.transcript_hash)
    try:
        pub = ed25519.Ed25519PublicKey.from_public_bytes(
            base64.b64decode(signed.signer_public_key)
        )
        pub.verify(base64.b64decode(signed.signature), msg)
    except Exception as e:
        return False, SIG_BAD_SIGNATURE, {"error": str(e)}

    # 4. Signer must be Tier K (declared in artifact AND confirmed by grant)
    if signed.signer_device_tier != "K":
        return False, SIG_WRONG_TIER, {
            "declared_tier": signed.signer_device_tier,
            "expected": "K",
        }

    signer_grant = grant_lookup(signed.signer_grant_id)
    if signer_grant is None:
        return False, SIG_GRANT_MISSING, {"grant_id": signed.signer_grant_id}

    if signer_grant.device_tier != "K":
        return False, SIG_WRONG_TIER, {
            "declared_tier": signed.signer_device_tier,
            "grant_tier": signer_grant.device_tier,
            "grant_id": signer_grant.grant_id,
        }

    if signer_grant.public_key != signed.signer_public_key:
        return False, SIG_PUBKEY_MISMATCH, {
            "signed_pubkey": signed.signer_public_key[:16] + "...",
            "grant_pubkey": signer_grant.public_key[:16] + "...",
        }

    # 5. Grant chain terminates at a trusted Tier-R pubkey
    tk_path = Path(trusted_keys_path)
    if not tk_path.exists():
        return False, SIG_TRUSTED_KEYS_MISSING, {"path": str(tk_path)}

    # Lazy import — only the verifier needs the chain walker, and the verifier
    # always runs in an env that has the storage.auth deps available.
    from storage.auth.grant import verify_grant_chain  # noqa: WPS433

    trusted = load_trusted_root_pubkeys(tk_path)
    ok, reason = verify_grant_chain(
        signer_grant,
        parent_lookup=grant_lookup,
        trusted_root_pubkeys=trusted,
    )
    if not ok:
        return False, SIG_CHAIN_INVALID, {
            "chain_reason": reason,
            "signer_grant_id": signer_grant.grant_id,
        }

    return True, SIG_OK, {
        "signer_device_id": signed.signer_device_id,
        "signer_grant_id": signed.signer_grant_id,
        "signed_at": signed.signed_at,
        "active_set_sha256": signed.active_set_sha256,
    }


# ── Convenience: build a grant_lookup callable from a grants dir ───────────

def make_grant_lookup_from_dir(grants_dir: Path) -> Callable[[str], Optional[Any]]:
    """Return a callable that maps grant_id -> DeviceGrant by scanning a
    directory of <device_id>.json grant files.

    The result indexes once at call-time; safe to invoke per boot.
    Lazy-imports DeviceGrant so this function is only callable in an env
    that has storage.auth available (i.e. the API container at boot).
    """
    from storage.auth.grant import DeviceGrant  # noqa: WPS433

    index: dict[str, Any] = {}
    d = Path(grants_dir)
    if d.exists():
        for p in d.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                g = DeviceGrant(**data)
                index[g.grant_id] = g
            except Exception:
                continue

    def _lookup(grant_id: str) -> Optional[Any]:
        return index.get(grant_id)

    return _lookup
