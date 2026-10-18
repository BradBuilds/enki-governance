# storage/auth/key_storage.py
"""
Encrypted key storage using AES-256-GCM.

Notes:
- Uses PBKDF2-HMAC-SHA256 for key derivation.
- File permission hardening is "best effort" on Windows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,          # AES-256
        salt=salt,
        iterations=600_000,
    )
    return kdf.derive(password.encode("utf-8"))


def save_encrypted_key(key_data: bytes, output_path: str, password: str) -> None:
    if password is None or password == "":
        raise RuntimeError(
            "Password is required to encrypt key. "
            "Set ENKI_ROOT_PASSWORD environment variable."
        )
    
    salt = os.urandom(32)
    nonce = os.urandom(12)  # AESGCM nonce size

    key = derive_key(password, salt)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, key_data, associated_data=None)

    envelope = {
        "kdf": "PBKDF2HMAC-SHA256",
        "iterations": 600_000,
        "salt": salt.hex(),
        "nonce": nonce.hex(),
        "ciphertext": ciphertext.hex(),
    }

    p = Path(output_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(envelope, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    # Best-effort permissions; Windows ACLs require operational handling.
    try:
        os.chmod(output_path, 0o400)
    except Exception:
        pass


def load_encrypted_key(key_path: str, password: str) -> bytes:
    if password is None or password == "":
        raise RuntimeError(
            "Password is required to decrypt key. "
            "Set ENKI_ROOT_PASSWORD environment variable."
        )
    
    envelope = json.loads(Path(key_path).read_text(encoding="utf-8"))

    salt = bytes.fromhex(envelope["salt"])
    nonce = bytes.fromhex(envelope["nonce"])
    ciphertext = bytes.fromhex(envelope["ciphertext"])

    key = derive_key(password, salt)
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ciphertext, associated_data=None)
