# storage/auth/__init__.py
"""
Phase 3.6 Device Authentication (EMULATED-capable).

This package implements:
- Root key (Ed25519) for signing DeviceGrants
- DeviceGrant issuance/verification with expiry + revocation
- Challenge-response authentication w/ replay prevention
- Grant persistence on filesystem (dev-friendly, deterministic format)
"""

