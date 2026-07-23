"""
ENKI Profile system — single source of truth for the operating posture.

A profile is one of: dev, cloud, hardware, hardened, hub.

Usage:
    from core.profile import resolve_profile, assert_valid

    profile, spec = resolve_profile()
    results = assert_valid(profile, spec)  # raises if misconfigured
    for r in results:
        log.info(str(r))

The single env var ENKI_PROFILE is the only thing operators set. Everything
else (whether to start Logto, whether to require HWKEY, etc.) follows from
the profile spec.
"""
from core.profile.spec import (
    Profile,
    ProfileSpec,
    PROFILES,
    get_spec,
    all_profiles,
)
from core.profile.resolver import (
    resolve_profile,
    is_dev,
    is_hub,
    ProfileResolutionError,
)
from core.profile.validator import (
    CheckResult,
    ProfileValidationError,
    validate,
    assert_valid,
)

__all__ = [
    "Profile",
    "ProfileSpec",
    "PROFILES",
    "get_spec",
    "all_profiles",
    "resolve_profile",
    "is_dev",
    "is_hub",
    "ProfileResolutionError",
    "CheckResult",
    "ProfileValidationError",
    "validate",
    "assert_valid",
]
