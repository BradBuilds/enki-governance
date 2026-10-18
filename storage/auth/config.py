# storage/auth/config.py
import os
from enum import Enum

class HardwareProfile(str, Enum):
    EMULATED = "EMULATED"       # software keys, NO attestation verification
    PRODUCTION = "PRODUCTION"   # hardware-backed keys + attestation verification

def _get_env(name: str, default: str | None = None) -> str | None:
    v = os.getenv(name)
    return v if v not in (None, "") else default

HARDWARE_PROFILE = HardwareProfile(_get_env("ENKI_HARDWARE_PROFILE", "EMULATED"))

# Root key configuration (Ed25519 root signer)
ROOT_KEY_PATH = _get_env("ENKI_ROOT_KEY_PATH", "storage/keys/root_key.enc")
ROOT_KEY_PASSWORD_ENV = _get_env("ENKI_ROOT_KEY_PASSWORD_ENV", "ENKI_ROOT_PASSWORD")
ROOT_KEY_PASSWORD = _get_env(ROOT_KEY_PASSWORD_ENV)

# Grants directory (file-backed)
GRANTS_DIR = _get_env("ENKI_GRANTS_DIR", "storage/grants")

# Challenge configuration
CHALLENGE_TTL_SECONDS = int(_get_env("ENKI_CHALLENGE_TTL_SECONDS", "300"))

# Attestation verification boundary
VERIFY_ATTESTATION = (HARDWARE_PROFILE == HardwareProfile.PRODUCTION)
