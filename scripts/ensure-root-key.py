#!/usr/bin/env python3
"""Ensure a software (emulated) root key exists for the self-host `dev` profile.

The dev profile permits an emulated ed25519 root key on disk (no hardware token).
This creates one on first boot if absent. Change ENKI_ROOT_PASSWORD for anything
beyond local self-host.
"""
import os

path = os.environ.get("ENKI_ROOT_KEY_PATH", "storage/keys/root_key.enc")
if os.path.exists(path):
    print(f"root key present at {path}")
else:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    from storage.auth.root_key import generate_root_key
    generate_root_key(path, os.environ.get("ENKI_ROOT_PASSWORD", "dev-root-password-change-me"))
    print(f"generated emulated root key at {path}")
