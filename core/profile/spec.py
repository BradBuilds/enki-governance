"""
Profile specifications — single source of truth for what each ENKI_PROFILE
requires and provides.

A profile is a complete operating posture. Choosing a profile determines:
  - Whether public access (Cloudflare Tunnel / Caddy) is provisioned
  - Whether a HWKEY hardware key is required
  - Whether LUKS disk encryption is required
  - Whether active_set / migrations must be signed
  - Which services run (workers, marketing, fleet)

User authentication is **not** a per-profile axis — every non-hub profile
uses self-hosted Logto OIDC. One auth flow, owned end-to-end. See
SETUP.md for an overview.

Canonical profiles (new model, the four that ship in public releases):
  - node          — default. Local-only Enki + Logto. The 95% case.
  - node-public   — node + Caddy + Cloudflare Tunnel for a public hostname.
  - evidentiary   — node + HWKEY + LUKS for court/regulatory deployments.
  - hub           — control plane (example.com).

Legacy profiles (deprecated, retained for in-place node-A compatibility):
  - dev           — old local-dev posture. Use `node` instead.
  - cloud         — old cloud-VM posture. Use `node` or `node-public`.
  - hardware      — old on-prem-with-local-auth. Use `evidentiary`.
  - hardened      — old on-prem-with-Logto. Use `evidentiary`.

The install script accepts both old and new names; the resolver normalizes
old → new internally. New installs should always use the new names.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import FrozenSet


class Profile(str, Enum):
    """Canonical operating profiles plus legacy aliases for backward compat."""

    # ── Current model (new operators use these) ────────────────────────────
    NODE = "node"
    """Default. Local-only Enki + Logto. The 95% case.
    Replaces the old DEV+CLOUD-without-public-hostname posture.
    Operators access at http://localhost:5173. Logto handles user auth.
    Software root key on disk."""

    NODE_PUBLIC = "node-public"
    """`node` + Caddy + Cloudflare Tunnel for a public hostname.
    For team/shared nodes or any operator wanting cross-network reach.
    Still uses Logto for user auth — Cloudflare is just plumbing."""

    NODE_LOCAL = "node-local"
    """`node` posture but with LOCAL email+password auth (no Logto, no
    Cloudflare Access) + optional TOTP for MFA. Zero external auth
    dependencies. Each node owns its own user database. Public
    hostname posture supported via Cloudflare Tunnel (network plumbing
    only). Software root key. The right answer for sovereignty-focused
    deployments that don't need HWKEY but want true zero-third-party
    auth."""

    EVIDENTIARY = "evidentiary"
    """`node` + HWKEY hardware key + LUKS disk encryption.
    For court / regulatory / chain-of-custody use cases.
    Most operators don't need this and shouldn't pick it."""

    HUB = "hub"
    """The control plane (example.com / marketing + fleet API VM).
    No customer data, no workers, no Logto. Hub admin token gates writes;
    public reads for marketing and fleet ingestion. One per company."""

    # ── Legacy aliases (existing node-A installs; do not use for new installs) ─
    DEV = "dev"
    """DEPRECATED: use NODE. Kept so existing node-A installs continue running."""

    CLOUD = "cloud"
    """DEPRECATED: use NODE (local) or NODE_PUBLIC (public hostname)."""

    HARDWARE = "hardware"
    """DEPRECATED: use EVIDENTIARY."""

    HARDENED = "hardened"
    """DEPRECATED: use EVIDENTIARY."""


@dataclass(frozen=True)
class ProfileSpec:
    """Declarative spec of what a profile requires and provides.

    Read-only at runtime. The server boot validator compares each requirement
    against actual system state and refuses to start if anything is unmet.
    """

    profile: Profile

    # ── User authentication ────────────────────────────────────────────────
    requires_logto: bool
    """If True, Logto OIDC must be reachable + LOGTO_APP_ID configured."""

    allows_local_users: bool
    """If True, the auth_local.py path (email+password JWT) is enabled.
    Mutually exclusive with requires_logto in normal use."""

    # ── Device / hardware key ──────────────────────────────────────────────
    requires_hardware_key: bool
    """If True, a HWKEY pubkey must be present in trusted_keys.signed
    and ENKI_HARDWARE_KEY_PATH must point at a working HWKEY adapter."""

    allows_emulated_root_key: bool
    """If True, software ed25519 root key in storage/keys/root_key.enc is OK.
    Mutually exclusive with requires_hardware_key."""

    # ── System integrity ───────────────────────────────────────────────────
    requires_luks: bool
    """If True, the postgres data volume must be a LUKS-mounted device."""

    requires_signed_active_set: bool
    """If True, active_set_v6.json must have a valid signature against
    a Tier K pubkey listed in trusted_keys.signed."""

    requires_signed_migrations: bool
    """If True, the migration manifest must be signed before enki-migrate.sh
    will apply any pending migration."""

    # ── Network ────────────────────────────────────────────────────────────
    enforce_tls: bool
    """If True, server refuses non-localhost requests on plaintext.
    Caddy/Traefik must be terminating TLS."""

    # ── Service composition ────────────────────────────────────────────────
    runs_workers: bool
    """If True, background extraction/enrichment/validation workers start."""

    runs_marketing_site: bool
    """If True, the marketing + landing pages are served (hub only)."""

    runs_fleet_api: bool
    """If True, fleet heartbeat ingestion is active (hub only)."""

    runs_ingestion: bool
    """If True, signal ingestion routes are active. False on hub."""

    # ── Allowed device tiers (for grant verification) ──────────────────────
    allowed_device_tiers: FrozenSet[str]
    """Which DeviceTiers can authenticate against this server.
    Tier R is never used at request time — only for issuing grants — so
    it never appears here."""

    # ── Description for operators ──────────────────────────────────────────
    description: str


PROFILES: dict[Profile, ProfileSpec] = {
    # ── Current model ──────────────────────────────────────────────────────
    Profile.NODE: ProfileSpec(
        profile=Profile.NODE,
        requires_logto=True,
        allows_local_users=False,
        requires_hardware_key=False,
        allows_emulated_root_key=True,
        requires_luks=False,
        requires_signed_active_set=True,
        requires_signed_migrations=True,
        enforce_tls=False,  # localhost-only access; Caddy not required
        runs_workers=True,
        runs_marketing_site=False,
        runs_fleet_api=False,
        runs_ingestion=True,
        allowed_device_tiers=frozenset({"K", "P", "W"}),
        description=(
            "Default operator install. Local-only access at "
            "http://localhost:5173. Users authenticate via the locally-running "
            "Logto instance (auto-bootstrapped during install). Software root "
            "key on disk. The 95% case — anyone running their own node "
            "without a public hostname or hardware key."
        ),
    ),
    Profile.NODE_PUBLIC: ProfileSpec(
        profile=Profile.NODE_PUBLIC,
        requires_logto=True,
        allows_local_users=False,
        requires_hardware_key=False,
        allows_emulated_root_key=True,
        requires_luks=False,
        requires_signed_active_set=True,
        requires_signed_migrations=True,
        enforce_tls=True,
        runs_workers=True,
        runs_marketing_site=False,
        runs_fleet_api=False,
        runs_ingestion=True,
        allowed_device_tiers=frozenset({"K", "P", "W"}),
        description=(
            "node + Caddy + Cloudflare Tunnel for a public hostname. "
            "For team/shared nodes or operators wanting cross-network reach. "
            "Logto still handles user auth — Cloudflare is just plumbing."
        ),
    ),
    Profile.NODE_LOCAL: ProfileSpec(
        profile=Profile.NODE_LOCAL,
        # User auth: local password + optional TOTP via /auth/local/*.
        # Logto NOT required; node owns its identities table.
        requires_logto=False,
        allows_local_users=True,
        # No hardware ceremony — software key is fine for this posture.
        requires_hardware_key=False,
        allows_emulated_root_key=True,
        requires_luks=False,
        requires_signed_active_set=False,
        requires_signed_migrations=False,
        # TLS is enforced when reachable publicly via Cf Tunnel; we leave
        # the env var as-is and let the operator set ENFORCE_TLS=1 if they
        # want server-side enforcement (Cf edge already terminates TLS).
        enforce_tls=False,
        runs_workers=True,
        runs_marketing_site=False,
        runs_fleet_api=False,
        runs_ingestion=True,
        allowed_device_tiers=frozenset({"K", "P", "W"}),
        description=(
            "Sovereignty-focused node. Local email+password auth (+ optional "
            "TOTP via Google Authenticator / Authy / 1Password / Bitwarden) "
            "for users — no Logto, no Cloudflare Access, no third party "
            "sees who is logging in. Each node owns its own identities "
            "table. Software root key. Public hostname posture supported "
            "via Cloudflare Tunnel as plumbing only (the Tunnel does NOT "
            "authenticate; auth is handled in-app). The right answer when "
            "HWKEY/LUKS ceremony is overkill but you still don't want "
            "any centralized identity provider."
        ),
    ),
    Profile.EVIDENTIARY: ProfileSpec(
        profile=Profile.EVIDENTIARY,
        requires_logto=True,
        allows_local_users=False,
        requires_hardware_key=True,
        allows_emulated_root_key=False,
        requires_luks=True,
        requires_signed_active_set=True,
        requires_signed_migrations=True,
        enforce_tls=True,
        runs_workers=True,
        runs_marketing_site=False,
        runs_fleet_api=False,
        runs_ingestion=True,
        allowed_device_tiers=frozenset({"K", "P", "W"}),
        description=(
            "node + HWKEY hardware key + LUKS disk encryption. For court, "
            "regulatory, or chain-of-custody deployments. Governance events "
            "(pack admission, migrations, code updates) require a physical "
            "button press on the HWKEY. Logto still handles user auth — "
            "HWKEY is for system operations."
        ),
    ),
    # ── Legacy / deprecated (kept so existing node-A installs keep running) ───
    Profile.DEV: ProfileSpec(
        profile=Profile.DEV,
        requires_logto=False,
        allows_local_users=False,
        requires_hardware_key=False,
        allows_emulated_root_key=True,
        requires_luks=False,
        requires_signed_active_set=False,
        requires_signed_migrations=False,
        enforce_tls=False,
        runs_workers=True,
        runs_marketing_site=False,
        runs_fleet_api=False,
        runs_ingestion=True,
        allowed_device_tiers=frozenset({"K", "P", "W"}),
        description=(
            "Local development. No auth on routes, header-based identity, "
            "software keys allowed. Refuses to start if production env "
            "variables (LOGTO_ENDPOINT, ENKI_HUB_URL, etc.) are set, to "
            "prevent dev mode in production."
        ),
    ),
    Profile.CLOUD: ProfileSpec(
        profile=Profile.CLOUD,
        requires_logto=True,
        allows_local_users=False,
        requires_hardware_key=False,
        allows_emulated_root_key=True,
        requires_luks=False,  # cloud provider's responsibility
        requires_signed_active_set=True,
        requires_signed_migrations=True,
        enforce_tls=True,
        runs_workers=True,
        runs_marketing_site=False,
        runs_fleet_api=False,
        runs_ingestion=True,
        allowed_device_tiers=frozenset({"K", "P", "W"}),
        description=(
            "Cloud node. Users authenticate via Logto OIDC. Devices "
            "authenticate via DeviceGrant chain (software root key OK). "
            "Active set and migrations must be signed. TLS enforced."
        ),
    ),
    Profile.HARDWARE: ProfileSpec(
        profile=Profile.HARDWARE,
        requires_logto=False,
        allows_local_users=True,
        requires_hardware_key=True,
        allows_emulated_root_key=False,
        requires_luks=True,
        requires_signed_active_set=True,
        requires_signed_migrations=True,
        enforce_tls=True,
        runs_workers=True,
        runs_marketing_site=False,
        runs_fleet_api=False,
        runs_ingestion=True,
        allowed_device_tiers=frozenset({"K", "P", "W"}),
        description=(
            "On-prem hardware. HWKEY required at boot to unlock LUKS "
            "and at every governance event (pack admission, migrations, "
            "updates). Local password auth for users — no Logto. The "
            "all-physical, all-sovereign mode."
        ),
    ),
    Profile.HARDENED: ProfileSpec(
        profile=Profile.HARDENED,
        requires_logto=True,
        allows_local_users=False,
        requires_hardware_key=True,
        allows_emulated_root_key=False,
        requires_luks=True,
        requires_signed_active_set=True,
        requires_signed_migrations=True,
        enforce_tls=True,
        runs_workers=True,
        runs_marketing_site=False,
        runs_fleet_api=False,
        runs_ingestion=True,
        allowed_device_tiers=frozenset({"K", "P", "W"}),
        description=(
            "On-prem with OIDC. Users via Logto; governance gated by "
            "HWKEY (Tier K signs pack admission, migrations, updates). "
            "LUKS required. For organizations that want SSO + physical "
            "governance gate."
        ),
    ),
    Profile.HUB: ProfileSpec(
        profile=Profile.HUB,
        requires_logto=False,
        allows_local_users=False,
        requires_hardware_key=False,
        allows_emulated_root_key=True,
        requires_luks=False,
        requires_signed_active_set=False,
        requires_signed_migrations=False,
        enforce_tls=True,
        runs_workers=False,
        runs_marketing_site=True,
        runs_fleet_api=True,
        runs_ingestion=False,
        allowed_device_tiers=frozenset({"P"}),  # P = federation peer grants
        description=(
            "Control plane / marketing site. No customer data, no "
            "workers, no user auth. Public reads. Admin operations "
            "gated by hub admin token (issued out-of-band)."
        ),
    ),
}


def get_spec(profile: Profile) -> ProfileSpec:
    """Look up the spec for a profile. Raises KeyError if unknown."""
    return PROFILES[profile]


def all_profiles() -> list[Profile]:
    """Return all known profiles in canonical order — new model first,
    legacy aliases last."""
    return [
        Profile.NODE,
        Profile.NODE_PUBLIC,
        Profile.EVIDENTIARY,
        Profile.HUB,
        # Legacy — deprecated but still resolvable for existing installs.
        Profile.DEV,
        Profile.CLOUD,
        Profile.HARDWARE,
        Profile.HARDENED,
    ]


# Migration map: deprecated profile → current equivalent. Used by the
# resolver to log a deprecation warning when an old name is in use,
# and by docs tooling to render the migration table.
DEPRECATED_PROFILES: dict[Profile, Profile] = {
    Profile.DEV: Profile.NODE,
    Profile.CLOUD: Profile.NODE,           # or NODE_PUBLIC if Cloudflare is wanted
    Profile.HARDWARE: Profile.EVIDENTIARY,
    Profile.HARDENED: Profile.EVIDENTIARY,
}


def current_profiles() -> list[Profile]:
    """Return only the current-model profiles (NODE, NODE_PUBLIC,
    EVIDENTIARY, HUB) — what new installs should choose from."""
    return [Profile.NODE, Profile.NODE_PUBLIC, Profile.EVIDENTIARY, Profile.HUB]
