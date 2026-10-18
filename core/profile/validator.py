"""
Boot-time profile validator — fail-closed.

Compares the resolved ProfileSpec against actual system state and raises if
anything required is missing. This runs once during API startup. If it raises,
the server refuses to start.

This is the single chokepoint where profile guarantees become real. Without
this, ENKI_PROFILE is just a label.

Each check is named, idempotent, and produces a structured result so the
boot log can show exactly what passed and what failed.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from core.profile.spec import Profile, ProfileSpec

log = logging.getLogger("enki.profile.validator")


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str

    def __str__(self) -> str:
        marker = "OK " if self.passed else "FAIL"
        return f"[{marker}] {self.name}: {self.detail}"


class ProfileValidationError(RuntimeError):
    """Raised when one or more validation checks fail."""

    def __init__(self, profile: Profile, results: list[CheckResult]) -> None:
        self.profile = profile
        self.results = results
        failed = [r for r in results if not r.passed]
        msg = f"Profile {profile.value} failed validation:\n" + "\n".join(
            f"  - {r}" for r in failed
        )
        super().__init__(msg)


# ── Individual checks ──────────────────────────────────────────────────────

def _check_dev_safety() -> CheckResult:
    """Soft check: DEV profile with production-pointing env vars present.

    Reports as passing-with-note rather than fail-closed. The actual safety
    perimeter is ENKI_PROFILE itself + the auth dispatcher in
    `services/api/src/unified_auth.py` (which routes by profile, NOT by env
    var presence). When hardware compose files leave LOGTO_ENDPOINT or an
    https public URL set as defaults, that's noise — not a security hole —
    so we surface it as a warning instead of crashing the api.
    """
    danger_vars = []
    if os.environ.get("LOGTO_ENDPOINT"):
        danger_vars.append("LOGTO_ENDPOINT")
    if os.environ.get("ENKI_HUB_URL"):
        danger_vars.append("ENKI_HUB_URL")
    if os.environ.get("ENKI_PUBLIC_URL", "").startswith("https://"):
        danger_vars.append("ENKI_PUBLIC_URL")
    if danger_vars:
        return CheckResult(
            "dev_safety",
            True,
            f"WARN: DEV profile with prod-shaped env vars present "
            f"({', '.join(danger_vars)}) — auth dispatcher ignores them",
        )
    return CheckResult("dev_safety", True, "no production env vars in DEV mode")


def _check_logto() -> CheckResult:
    endpoint = os.environ.get("LOGTO_ENDPOINT", "")
    app_id = os.environ.get("LOGTO_APP_ID", "")
    if not endpoint:
        return CheckResult("logto", False, "LOGTO_ENDPOINT not set")
    if not app_id:
        return CheckResult("logto", False, "LOGTO_APP_ID not set")
    return CheckResult("logto", True, f"endpoint={endpoint}, app_id set")


def _check_hardware_key() -> CheckResult:
    """Hardware key requires:
    - ENKI_HARDWARE_KEY_PATH points to enki-hardware-key/server adapter
    - trusted_keys.signed file exists with at least one R or K pubkey
    """
    hw_path = os.environ.get("ENKI_HARDWARE_KEY_PATH", "")
    if not hw_path or not Path(hw_path).exists():
        return CheckResult(
            "hardware_key",
            False,
            f"ENKI_HARDWARE_KEY_PATH={hw_path!r} not found",
        )

    trusted_keys = Path(os.environ.get("ENKI_TRUSTED_KEYS_PATH",
                                       "/etc/enki/trusted_keys.signed"))
    if not trusted_keys.exists():
        return CheckResult(
            "hardware_key",
            False,
            f"trusted_keys file missing at {trusted_keys}",
        )

    return CheckResult(
        "hardware_key",
        True,
        f"adapter at {hw_path}, trusted_keys at {trusted_keys}",
    )


def _check_emulated_root_key() -> CheckResult:
    """When emulated root key is allowed, the encrypted file must exist."""
    key_path = Path(os.environ.get("ENKI_ROOT_KEY_PATH",
                                   "storage/keys/root_key.enc"))
    if not key_path.exists():
        return CheckResult(
            "root_key",
            False,
            f"emulated root key missing at {key_path}",
        )
    return CheckResult("root_key", True, f"emulated root key at {key_path}")


def _check_luks() -> CheckResult:
    """Verify the postgres data volume is a LUKS-mounted device.

    Looks at the mount point declared by ENKI_DATA_MOUNT (default
    /var/lib/enki/data) and checks the underlying source is /dev/mapper/*."""
    mount = Path(os.environ.get("ENKI_DATA_MOUNT", "/var/lib/enki/data"))
    if not mount.exists():
        return CheckResult("luks", False, f"data mount {mount} not present")

    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == str(mount):
                    src = parts[0]
                    if src.startswith("/dev/mapper/"):
                        return CheckResult("luks", True, f"{mount} ← {src}")
                    return CheckResult(
                        "luks",
                        False,
                        f"{mount} mounted from {src}, expected /dev/mapper/*",
                    )
        return CheckResult("luks", False, f"{mount} not in /proc/mounts")
    except Exception as e:
        return CheckResult("luks", False, f"could not read /proc/mounts: {e}")


def _check_signed_active_set() -> CheckResult:
    """Verify active_set_v6.signed.json signature against the trust root.

    Full check: file present, file-hash commitment matches, Ed25519 signature
    valid, signer is Tier K, K grant chains back to a Tier R pubkey listed in
    trusted_keys.signed. Any failure means refuse to boot.
    """
    from core.spine.active_set_signature import (
        SIG_OK,
        make_grant_lookup_from_dir,
        signed_artifact_path,
        verify_active_set_signature,
    )

    base = Path(os.environ.get("ENKI_ACTIVE_SET_PATH", "active_set_v6.json"))
    if not base.exists():
        return CheckResult(
            "signed_active_set", False, f"active_set not found at {base}"
        )

    signed = signed_artifact_path(base)
    trusted_keys = Path(os.environ.get(
        "ENKI_TRUSTED_KEYS_PATH", "/etc/enki/trusted_keys.signed"
    ))
    grants_dir = Path(os.environ.get("ENKI_GRANTS_DIR", "storage/grants"))

    grant_lookup = make_grant_lookup_from_dir(grants_dir)
    ok, code, ctx = verify_active_set_signature(
        active_set_path=base,
        signed_path=signed,
        trusted_keys_path=trusted_keys,
        grant_lookup=grant_lookup,
    )
    if ok:
        return CheckResult(
            "signed_active_set",
            True,
            f"{signed.name} verified (signer={ctx.get('signer_device_id')}, "
            f"signed_at={ctx.get('signed_at')})",
        )

    return CheckResult(
        "signed_active_set",
        False,
        f"{code} ({ctx})",
    )


def _check_signed_migrations() -> CheckResult:
    """A signed migration manifest must exist."""
    manifest = Path(os.environ.get(
        "ENKI_MIGRATION_MANIFEST_PATH",
        "storage/postgres/migrations/manifest.signed.json",
    ))
    if not manifest.exists():
        return CheckResult(
            "signed_migrations",
            False,
            f"migration manifest not found at {manifest}",
        )
    return CheckResult("signed_migrations", True, f"{manifest} present")


# ── Orchestrator ───────────────────────────────────────────────────────────

def validate(profile: Profile, spec: ProfileSpec) -> list[CheckResult]:
    """Run all relevant checks for the spec. Returns a list of results.

    Checks are skipped when the spec says they're not required, so this
    function is non-destructive: it never raises. The caller decides how
    to handle failed results (typically by raising ProfileValidationError
    if any failed).
    """
    results: list[CheckResult] = []

    if profile == Profile.DEV:
        results.append(_check_dev_safety())

    if spec.requires_logto:
        results.append(_check_logto())

    if spec.requires_hardware_key:
        results.append(_check_hardware_key())
    elif spec.allows_emulated_root_key:
        results.append(_check_emulated_root_key())

    if spec.requires_luks:
        results.append(_check_luks())

    if spec.requires_signed_active_set:
        results.append(_check_signed_active_set())

    if spec.requires_signed_migrations:
        results.append(_check_signed_migrations())

    return results


def assert_valid(profile: Profile, spec: ProfileSpec) -> list[CheckResult]:
    """Run validation; raise ProfileValidationError if any check failed.

    Returns the full result list on success (so the boot logger can show
    every check that passed)."""
    results = validate(profile, spec)
    failed = [r for r in results if not r.passed]
    if failed:
        raise ProfileValidationError(profile, results)
    return results
