"""
Profile resolver — reads ENKI_PROFILE from env, validates it's a known
profile, and returns the resolved Profile + ProfileSpec.

Resolution order (highest priority first):
  1. ENKI_PROFILE environment variable
  2. /etc/enki/enki.conf  (key=value lines)
  3. Default: refuse to start. Profile must be explicit.

There is no automatic fallback to "dev" in production paths. A missing
ENKI_PROFILE is a configuration error, not a default.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from core.profile.spec import Profile, ProfileSpec, get_spec, DEPRECATED_PROFILES

log = logging.getLogger("enki.profile.resolver")

ENKI_CONF_PATH = Path(os.environ.get("ENKI_CONF_PATH", "/etc/enki/enki.conf"))


class ProfileResolutionError(RuntimeError):
    """Raised when ENKI_PROFILE cannot be resolved or is invalid."""


def _read_conf_file(path: Path) -> dict[str, str]:
    """Read a simple KEY=VALUE config file, ignoring comments and blanks."""
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def resolve_profile(*, allow_dev_default: bool = False) -> tuple[Profile, ProfileSpec]:
    """Resolve the active profile from env or /etc/enki/enki.conf.

    Args:
        allow_dev_default: If True, fall back to DEV profile when nothing
            is set. Reserved for unit tests and local Python sessions —
            never used in service boot paths.

    Returns:
        (Profile, ProfileSpec) tuple.

    Raises:
        ProfileResolutionError: If no profile is set, or the value is
            not one of the five canonical profiles.
    """
    raw: Optional[str] = os.environ.get("ENKI_PROFILE")

    if not raw and ENKI_CONF_PATH.exists():
        conf = _read_conf_file(ENKI_CONF_PATH)
        raw = conf.get("ENKI_PROFILE")

    if not raw:
        if allow_dev_default:
            log.warning("ENKI_PROFILE unset; defaulting to NODE (allow_dev_default=True)")
            return Profile.NODE, get_spec(Profile.NODE)
        raise ProfileResolutionError(
            "ENKI_PROFILE is not set. Profile must be one of: "
            "node, node-public, evidentiary, hub. "
            f"Looked at env and {ENKI_CONF_PATH}."
        )

    raw = raw.strip().lower()
    try:
        profile = Profile(raw)
    except ValueError:
        raise ProfileResolutionError(
            f"ENKI_PROFILE={raw!r} is not a valid profile. "
            "Valid profiles: node, node-public, evidentiary, hub "
            "(deprecated aliases also accepted: dev, cloud, hardware, hardened)."
        )

    # Log deprecation warning for legacy profile names so operators know
    # to migrate. The legacy profiles still work — they have their own
    # ProfileSpec entries — but emit one warning at boot so it's visible
    # in logs.
    if profile in DEPRECATED_PROFILES:
        replacement = DEPRECATED_PROFILES[profile]
        log.warning(
            "ENKI_PROFILE=%s is deprecated. Use ENKI_PROFILE=%s for new "
            "installs. See SETUP.md.",
            profile.value, replacement.value,
        )

    return profile, get_spec(profile)


def is_dev() -> bool:
    """Quick check if current profile is the legacy DEV alias.
    Most checks should ask whether the profile *requires* something
    (Logto, signed pack, etc.) via the ProfileSpec rather than name-match.
    Never raises."""
    try:
        profile, _ = resolve_profile()
        return profile == Profile.DEV
    except ProfileResolutionError:
        return False


def is_hub() -> bool:
    """Quick check if current profile is HUB."""
    try:
        profile, _ = resolve_profile()
        return profile == Profile.HUB
    except ProfileResolutionError:
        return False
