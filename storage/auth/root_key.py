# storage/auth/root_key.py
"""
Root signer key operations (Ed25519).

- Root key signs DeviceGrants (authorization artifacts).
- In EMULATED: root key is stored as AES-256-GCM encrypted file.
- In PRODUCTION: you may keep the same interface but change storage backend.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization

from storage.auth.key_storage import load_encrypted_key, save_encrypted_key


@dataclass(frozen=True)
class RootKey:
    private_key: ed25519.Ed25519PrivateKey

    @property
    def public_key(self) -> ed25519.Ed25519PublicKey:
        return self.private_key.public_key()

    def sign(self, message: bytes) -> bytes:
        return self.private_key.sign(message)

    def public_key_bytes_raw(self) -> bytes:
        return self.public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    def public_key_base64(self) -> str:
        return base64.b64encode(self.public_key_bytes_raw()).decode("ascii")


def generate_root_key(output_path: str, password: str) -> RootKey:
    private_key = ed25519.Ed25519PrivateKey.generate()

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    save_encrypted_key(private_bytes, output_path, password)
    return RootKey(private_key)


def load_root_key(key_path: str, password: str) -> RootKey:
    private_bytes = load_encrypted_key(key_path, password)

    private_key = serialization.load_pem_private_key(private_bytes, password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise ValueError("Root key is not Ed25519")

    return RootKey(private_key)

