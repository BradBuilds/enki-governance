#!/usr/bin/env python3
"""
Issue a Tier-K signer for this install — software key, no hardware required.

The Tier-K signer is the key that signs the active_set. Until now the only way
to get one was to provision a HWKEY over USB from the enki-hardware-key repo,
which made a signed active_set impossible for anyone self-hosting without the
hardware. This issues the same grant against a password-sealed software key.

What it does:
  1. Generate an Ed25519 keypair, sealed with AES-256-GCM under a password
  2. Issue a Tier-K DeviceGrant for it, signed by the Tier-R root
  3. Save the grant next to the others so the boot-time verifier finds it

The security tradeoff is real and worth stating plainly: a software key can be
copied by anything that can read the file and the password, where a hardware key
cannot leave the device and requires a physical button press per signature. For
a personal or team node that is the right trade. For the `hardened` and
`evidentiary` profiles it is not — use the HWKEY there.

Usage:
    python scripts/governance/issue_signer_key.py
    python scripts/governance/issue_signer_key.py --device-id my-node-signer
    python scripts/governance/issue_signer_key.py --rotate     # replace an existing signer

Environment:
    ENKI_ROOT_PASSWORD      required — unseals the Tier-R root key
    ENKI_SIGNER_PASSWORD    password for the new signer key (defaults to ENKI_ROOT_PASSWORD)
    ENKI_SIGNER_KEY_PATH    default storage/keys/signer_key.enc
    ENKI_ROOT_KEY_PATH      default storage/keys/root_key.enc
    ENKI_GRANTS_DIR         default storage/grants
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from storage.auth.config import GRANTS_DIR, ROOT_KEY_PATH
from storage.auth.grant import issue_child_grant
from storage.auth.grant_store import GrantStore
from storage.auth.root_key import generate_signer_key, load_root_key, load_signer_key

DEFAULT_SIGNER_KEY_PATH = "storage/keys/signer_key.enc"
DEFAULT_VALIDITY_DAYS = 365


def _find_root_grant(store: GrantStore):
    """Return the Tier-R grant for this install, or None."""
    for grant in store.load_all().values():
        if grant.device_tier == "R" and not grant.revoked_at:
            return grant
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Issue a Tier-K software signer grant chained to the Tier-R root.")
    ap.add_argument("--device-id", default="local-signer",
                    help="Identifier for this signer (default: local-signer)")
    ap.add_argument("--key-path",
                    default=os.environ.get("ENKI_SIGNER_KEY_PATH", DEFAULT_SIGNER_KEY_PATH),
                    help=f"Where to write the sealed signer key (default: {DEFAULT_SIGNER_KEY_PATH})")
    ap.add_argument("--grants-dir", default=os.environ.get("ENKI_GRANTS_DIR", GRANTS_DIR))
    ap.add_argument("--root-key-path", default=os.environ.get("ENKI_ROOT_KEY_PATH", ROOT_KEY_PATH))
    ap.add_argument("--days", type=int, default=DEFAULT_VALIDITY_DAYS,
                    help=f"Grant validity in days (default: {DEFAULT_VALIDITY_DAYS})")
    ap.add_argument("--tenant-scopes", default="default",
                    help="Comma-separated tenant scopes (default: default)")
    ap.add_argument("--rotate", action="store_true",
                    help="Replace an existing signer key/grant with the same device-id")
    args = ap.parse_args()

    root_password = os.environ.get("ENKI_ROOT_PASSWORD")
    if not root_password:
        print("ERROR: ENKI_ROOT_PASSWORD is not set — it unseals the Tier-R root key.",
              file=sys.stderr)
        print("  If you have not created a trust root yet, run:", file=sys.stderr)
        print("    python scripts/governance/bootstrap_trust_root.py", file=sys.stderr)
        return 1
    signer_password = os.environ.get("ENKI_SIGNER_PASSWORD") or root_password

    key_path = Path(args.key_path)
    if key_path.exists() and not args.rotate:
        print(f"ERROR: a signer key already exists at {key_path}.", file=sys.stderr)
        print("  Pass --rotate to replace it (the old grant stays on disk and can be revoked).",
              file=sys.stderr)
        return 1

    store = GrantStore(str(args.grants_dir))
    root_grant = _find_root_grant(store)
    if root_grant is None:
        print(f"ERROR: no active Tier-R grant found in {args.grants_dir}.", file=sys.stderr)
        print("  Create the trust root first:", file=sys.stderr)
        print("    python scripts/governance/bootstrap_trust_root.py", file=sys.stderr)
        return 1

    try:
        root_key = load_root_key(str(args.root_key_path), root_password)
    except Exception as e:
        print(f"ERROR: could not unseal the root key at {args.root_key_path}: {e}",
              file=sys.stderr)
        print("  Check ENKI_ROOT_PASSWORD.", file=sys.stderr)
        return 1

    # The root grant on disk must actually correspond to the root key we just
    # unsealed, otherwise we would issue a grant that no verifier can chain.
    if root_key.public_key_base64() != root_grant.public_key:
        print("ERROR: the root key does not match the Tier-R grant on disk.", file=sys.stderr)
        print(f"  key  pubkey: {root_key.public_key_base64()}", file=sys.stderr)
        print(f"  grant pubkey: {root_grant.public_key}", file=sys.stderr)
        print("  These must match or the signature chain will not verify.", file=sys.stderr)
        return 1

    key_path.parent.mkdir(parents=True, exist_ok=True)
    signer_key = generate_signer_key(str(key_path), signer_password)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass

    expires_at = (datetime.now(timezone.utc) + timedelta(days=args.days)).isoformat()
    grant = issue_child_grant(
        device_id=args.device_id,
        public_key_bytes=signer_key.public_key_bytes_raw(),
        public_key_algorithm="Ed25519",
        device_tier="K",
        tenant_scopes=[s.strip() for s in args.tenant_scopes.split(",") if s.strip()],
        expires_at=expires_at,
        parent_grant=root_grant,
        parent_signer=root_key,
    )
    store.save(grant)
    grant_path = Path(args.grants_dir) / f"{args.device_id}.json"

    print()
    print("  Tier-K signer issued.")
    print()
    print(f"    device_id:   {grant.device_id}")
    print(f"    tier:        {grant.device_tier}  (chained to Tier-R '{root_grant.device_id}')")
    print(f"    public key:  {grant.public_key}")
    print(f"    grant:       {grant_path}")
    print(f"    sealed key:  {key_path}  (mode 600)")
    print(f"    expires:     {expires_at}")
    print()
    print("  Next: sign the active set with it —")
    print(f"    python scripts/governance/sign_active_set.py active_set_v6.json \\")
    print(f"        --ceremony-dir storage/admission/v1 \\")
    print(f"        --signer-grant {grant_path} \\")
    print(f"        --signer-key {key_path}")
    print()
    print("  Keep the sealed key and its password backed up. Losing them means")
    print("  re-issuing a signer and re-signing; losing the ROOT key means")
    print("  rebuilding the trust root from genesis.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
