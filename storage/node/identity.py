"""
Node identity generation and management.

Every Enki server is a node. At install time, it generates:
  - node_id: "enki_<8-hex-chars>" (globally unique, underscore-separated)
  - Ed25519 keypair: signs inter-node messages (separate from root key)

Per FD-2: node_id appears in every signal, receipt, and chain entry.
Per FD-6: IDs use underscore format (compatible with existing schema regex).

Usage:
    from storage.node.identity import generate_node_identity, load_node_id

    # First time (install):
    identity = generate_node_identity(
        display_name="Smith Family Server",
        key_path="/var/lib/enki/keys/node_key.enc",
        password="...",
    )
    # identity.node_id = "enki_a3f7b2c1"

    # Runtime:
    node_id = load_node_id()  # from env or config
"""

import hashlib
import os
import base64
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization


@dataclass
class NodeIdentity:
    """Immutable node identity. Generated once at install time."""
    node_id: str                  # "enki_<8-hex>"
    display_name: str
    public_key_b64: str           # Ed25519 public key, base64
    public_key_bytes: bytes       # raw 32 bytes


def generate_node_id() -> str:
    """
    Generate a globally unique node_id.

    Format: "enki_<8-hex-chars>"
    Derived from: SHA-256 of 32 random bytes, take first 8 hex chars.
    Collision probability: ~1 in 4 billion.
    Underscore-separated per FD-6 (compatible with existing ID regex).
    """
    random_bytes = os.urandom(32)
    hash_hex = hashlib.sha256(random_bytes).hexdigest()
    short = hash_hex[:8]
    return f"enki_{short}"


def generate_node_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """Generate an Ed25519 keypair for node identity."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return private_key, public_key


def export_public_key_b64(public_key: Ed25519PublicKey) -> str:
    """Export Ed25519 public key as base64 string."""
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def generate_node_identity(
    display_name: str,
    key_path: Optional[str] = None,
    password: Optional[str] = None,
) -> NodeIdentity:
    """
    Generate a complete node identity (node_id + Ed25519 keypair).

    Args:
        display_name: Human-readable name for this node.
        key_path: If provided, save encrypted private key to this path.
        password: Password for encrypting the private key file.

    Returns:
        NodeIdentity with node_id, display_name, and public key.
    """
    node_id = generate_node_id()
    private_key, public_key = generate_node_keypair()

    pub_b64 = export_public_key_b64(public_key)
    pub_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    if key_path and password:
        _save_private_key(private_key, key_path, password)

    return NodeIdentity(
        node_id=node_id,
        display_name=display_name,
        public_key_b64=pub_b64,
        public_key_bytes=pub_bytes,
    )


def _save_private_key(
    private_key: Ed25519PrivateKey,
    path: str,
    password: str,
) -> None:
    """Save encrypted Ed25519 private key to disk."""
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.BestAvailableEncryption(
            password.encode("utf-8")
        ),
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(pem)
    os.chmod(path, 0o600)


def load_node_id() -> str:
    """
    Load node_id from environment variable ENKI_NODE_ID.

    This is set during installation and stored in /etc/enki/enki.conf.
    Every signal, receipt, and chain entry uses this value.

    Raises:
        EnvironmentError: If ENKI_NODE_ID is not set.
    """
    node_id = os.environ.get("ENKI_NODE_ID")
    if not node_id:
        raise EnvironmentError(
            "ENKI_NODE_ID environment variable not set. "
            "This should be set during installation. "
            "Run: enki-ctl node init"
        )
    return node_id


def load_node_private_key(
    key_path: str,
    password: str,
) -> Ed25519PrivateKey:
    """Load encrypted Ed25519 node private key from disk."""
    with open(key_path, "rb") as f:
        pem = f.read()
    private_key = serialization.load_pem_private_key(
        pem,
        password=password.encode("utf-8"),
    )
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("Node key is not Ed25519")
    return private_key


def register_node_in_db(identity: NodeIdentity, dsn: str) -> None:
    """
    Register this node's identity in the node_identity table.

    Called once during installation. Sets is_self=TRUE for this node.
    """
    from storage.postgres.db import get_connection

    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO node_identity
                    (node_id, display_name, public_key, is_self)
                VALUES (%s, %s, %s, TRUE)
                ON CONFLICT (node_id) DO NOTHING
            """, (
                identity.node_id,
                identity.display_name,
                identity.public_key_b64,
            ))
        conn.commit()
