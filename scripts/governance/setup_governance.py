#!/usr/bin/env python3
"""
Set up pack governance for this install — one command, no hardware.

Runs the whole integrity chain and stops with a clear message the moment
anything is off:

    trust root  →  Tier-K signer  →  admission ceremony  →  signature  →  verify

Each step is skipped if it is already done, so this is safe to re-run after
changing a pack: it will notice the active_set hash moved, re-run the ceremony,
and re-sign.

    python scripts/governance/setup_governance.py

    # non-interactive (CI, provisioning):
    ENKI_ROOT_PASSWORD=... python scripts/governance/setup_governance.py --yes

    # after editing packs — refresh hashes, re-run ceremony, re-sign:
    python scripts/governance/setup_governance.py --resign

What you end up with:
    storage/keys/root_key.enc        Tier-R root key   (sealed)
    storage/keys/signer_key.enc      Tier-K signer key (sealed)
    storage/grants/*.json            the grant chain
    /etc/enki/trusted_keys.signed    the Tier-R pubkey the verifier trusts
    active_set_v6.signed.json        the signature

Only the last one is needed at boot; the rest is what proves it.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

GOV = _PROJECT_ROOT / "scripts" / "governance"
DEFAULT_ACTIVE_SET = "active_set_v6.json"
DEFAULT_CEREMONY_DIR = "storage/admission/v1"
DEFAULT_SIGNER_KEY = "storage/keys/signer_key.enc"


def _step(n: int, total: int, msg: str) -> None:
    # flush: subprocesses write straight to the fd, so an unflushed parent
    # print would surface AFTER the output of the step it introduces.
    print(f"\n[{n}/{total}] {msg}", flush=True)


def _run(argv: list[str], env: dict) -> int:
    return subprocess.call([sys.executable] + argv, cwd=str(_PROJECT_ROOT), env=env)


def _declared_root(trusted_keys_path: Path):
    """Return the first Tier-R entry declared in trusted_keys.signed, if any."""
    if not trusted_keys_path.exists():
        return None
    try:
        data = json.loads(trusted_keys_path.read_text())
    except Exception:
        return None
    for entry in data.get("trusted_root_pubkeys") or []:
        out = dict(entry)
        out["path"] = str(trusted_keys_path)
        return out
    return None


def _all_grants(grants_dir: Path, tier: str) -> list[tuple[Path, dict]]:
    """Every active grant of `tier`, as (path, grant_dict)."""
    out: list[tuple[Path, dict]] = []
    if not grants_dir.is_dir():
        return out
    for p in sorted(grants_dir.glob("*.json")):
        try:
            g = json.loads(p.read_text())
        except Exception:
            continue
        if g.get("device_tier") == tier and not g.get("revoked_at"):
            out.append((p, g))
    return out


def _find_grant(grants_dir: Path, tier: str, parent_public_key: str | None = None):
    """Return (path, grant_dict) for an active grant of `tier`.

    When `parent_public_key` is given, prefer a grant that actually chains to
    it. Installs accumulate stale practice/bootstrap grants, and picking the
    first filename alphabetically silently selects the wrong one.
    """
    grants = _all_grants(grants_dir, tier)
    if not grants:
        return None, None
    if parent_public_key:
        for p, g in grants:
            if g.get("parent_public_key") == parent_public_key:
                return p, g
    return grants[0]


def _status(active_set: Path, grants_dir: Path, signer_key: Path) -> int:
    """Report the governance state without touching anything.

    The awkward case this exists for: an install that declares a trust root in
    trusted_keys.signed but has no Tier-R grant on disk, so you cannot tell by
    inspection whether the sealed root key it *does* have corresponds to that
    declared root or to some other one. Unsealing needs the password, so we
    report exactly what is knowable and name what to check.
    """
    tk_path = Path(os.environ.get("ENKI_TRUSTED_KEYS_PATH", "/etc/enki/trusted_keys.signed"))
    root_key_path = Path(os.environ.get("ENKI_ROOT_KEY_PATH", "storage/keys/root_key.enc"))
    declared = _declared_root(tk_path)
    _, root_grant = _find_grant(grants_dir, "R")
    root_pub = (root_grant or {}).get("public_key") or (declared or {}).get("public_key")
    k_grants = _all_grants(grants_dir, "K")
    k_path, k_grant = _find_grant(grants_dir, "K", parent_public_key=root_pub)

    print("\n  Governance status")
    print("  " + "-" * 62)
    print(f"  active set          {active_set}"
          f"{'' if active_set.exists() else '   (MISSING)'}")
    print(f"  trusted_keys        {tk_path}"
          f"{'' if tk_path.exists() else '   (not present)'}")
    if declared:
        print(f"    declares root     {declared.get('device_id')}  {declared.get('public_key')}")
    print(f"  root key (sealed)   {root_key_path}"
          f"{'' if root_key_path.exists() else '   (not present)'}")
    print(f"  Tier-R grant        {root_grant['device_id'] if root_grant else 'NONE on disk'}")
    if root_grant:
        print(f"    public key        {root_grant['public_key']}")
    if not k_grants:
        print("  Tier-K signer       NONE on disk")
    else:
        print(f"  Tier-K grants       {len(k_grants)} on disk")
        for p, g in k_grants:
            chains = g.get("parent_public_key")
            mark = "usable " if chains and chains == root_pub else "stale  "
            print(f"    [{mark}] {g['device_id']:<20} chains to {chains}")
    print(f"  signer key (sealed) {signer_key}"
          f"{'' if signer_key.exists() else '   (not present)'}")

    from core.spine.active_set_signature import signed_artifact_path
    sp = signed_artifact_path(active_set)
    print(f"  signature           {sp}{'' if sp.exists() else '   (not signed)'}")

    print("\n  Assessment")
    print("  " + "-" * 62)
    if root_grant and k_grant and signer_key.exists() and sp.exists():
        print("  Looks complete. Verify it with:  setup_governance.py  (re-runs and checks)")
        return 0
    if declared and not root_grant:
        print("  A trust root is DECLARED but its Tier-R grant is not on disk.")
        if root_key_path.exists():
            print(f"  There IS a sealed root key at {root_key_path}, but without the")
            print("  password we cannot tell whether it belongs to the declared root")
            print(f"  ({declared.get('device_id')}) or to a different one.")
            if k_grant and k_grant.get("parent_public_key") != declared.get("public_key"):
                print()
                print("  Note: the Tier-K grant on disk chains to")
                print(f"    {k_grant.get('parent_public_key')}")
                print("  which is NOT the declared root")
                print(f"    {declared.get('public_key')}")
                print("  so at least one of them is stale.")
            print()
            print("  To find out, unseal it and compare:")
            print("    ENKI_ROOT_PASSWORD=... python -c \\")
            print("      \"from storage.auth.root_key import load_root_key; \\")
            print("       import os; print(load_root_key(os.environ['ENKI_ROOT_KEY_PATH'], \\")
            print("       os.environ['ENKI_ROOT_PASSWORD']).public_key_base64())\"")
            print()
            print("  If it matches the declared root, re-issue a signer and you're done:")
            print("    python scripts/governance/issue_signer_key.py")
            print("  If it does not, start a fresh trust root (replaces the declared one):")
            print("    python scripts/governance/bootstrap_trust_root.py --force-genesis")
        else:
            print("  No sealed root key either — start a fresh trust root:")
            print("    python scripts/governance/bootstrap_trust_root.py --force-genesis")
        return 1
    print("  Not set up yet. Run:  python scripts/governance/setup_governance.py")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Set up pack governance (no hardware required).")
    ap.add_argument("--active-set", default=DEFAULT_ACTIVE_SET)
    ap.add_argument("--ceremony-dir", default=DEFAULT_CEREMONY_DIR)
    ap.add_argument("--grants-dir", default=os.environ.get("ENKI_GRANTS_DIR", "storage/grants"))
    ap.add_argument("--signer-key", default=os.environ.get("ENKI_SIGNER_KEY_PATH", DEFAULT_SIGNER_KEY))
    ap.add_argument("--device-id", default="local-signer")
    ap.add_argument("--operator", default=os.environ.get("USER", "operator"))
    ap.add_argument("--resign", action="store_true",
                    help="Re-run the ceremony and re-sign even if a signature exists")
    ap.add_argument("--yes", action="store_true", help="Do not prompt (requires ENKI_ROOT_PASSWORD)")
    ap.add_argument("--status", action="store_true",
                    help="Report what governance state this install is in, change nothing")
    args = ap.parse_args()

    if args.status:
        return _status(Path(args.active_set), Path(args.grants_dir), Path(args.signer_key))

    active_set = Path(args.active_set)
    if not active_set.exists():
        print(f"ERROR: active set not found: {active_set}", file=sys.stderr)
        return 1
    grants_dir = Path(args.grants_dir)
    signer_key = Path(args.signer_key)
    ceremony_dir = Path(args.ceremony_dir)

    env = dict(os.environ)
    password = env.get("ENKI_ROOT_PASSWORD")
    if not password:
        if args.yes:
            print("ERROR: --yes requires ENKI_ROOT_PASSWORD to be set.", file=sys.stderr)
            return 1
        print("\nThis install needs a password to seal its signing keys.")
        print("Keep it safe — losing it means rebuilding the trust root.\n")
        password = getpass.getpass("  Password: ")
        confirm = getpass.getpass("  Confirm:  ")
        if password != confirm:
            print("ERROR: passwords did not match.", file=sys.stderr)
            return 1
        if not password:
            print("ERROR: password cannot be empty.", file=sys.stderr)
            return 1
    env["ENKI_ROOT_PASSWORD"] = password
    env.setdefault("ENKI_SIGNER_PASSWORD", password)

    TOTAL = 5

    # ── 1. Trust root ──────────────────────────────────────────────────
    _step(1, TOTAL, "Trust root (Tier R)")
    root_path, root_grant = _find_grant(grants_dir, "R")
    if root_grant:
        print(f"      already present: {root_grant['device_id']}  ({root_path})", flush=True)
    else:
        # An install can declare a trust root in trusted_keys.signed while having
        # no Tier-R grant (and no private key) on disk — e.g. the root was created
        # elsewhere, or a hardware ceremony provisioned it. bootstrap_trust_root
        # will refuse to overwrite it, which reads as a baffling failure unless we
        # name the situation first.
        declared = _declared_root(Path(os.environ.get(
            "ENKI_TRUSTED_KEYS_PATH", "/etc/enki/trusted_keys.signed")))
        if declared:
            print(f"      {declared['path']} already declares a trust root:", flush=True)
            print(f"        device_id:  {declared.get('device_id')}")
            print(f"        public key: {declared.get('public_key')}")
            print(f"      ...but there is no Tier-R grant in {grants_dir}, so this")
            print("      install cannot issue signers against it.")
            print()
            print("      Either restore that root's grant + private key, or start a new")
            print("      trust root (this REPLACES the declared one, and anything signed")
            print("      under it stops verifying):")
            print()
            print("        python scripts/governance/bootstrap_trust_root.py --force-genesis")
            print("        python scripts/governance/setup_governance.py")
            print()
            return 1
        rc = _run([str(GOV / "bootstrap_trust_root.py")], env)
        if rc != 0:
            print("\nERROR: could not create the trust root.", file=sys.stderr)
            return rc
        root_path, root_grant = _find_grant(grants_dir, "R")
        if not root_grant:
            print("\nERROR: trust root bootstrap reported success but no Tier-R grant "
                  "was written.", file=sys.stderr)
            return 1

    # ── 2. Tier-K signer ───────────────────────────────────────────────
    _step(2, TOTAL, "Signer (Tier K)")
    k_path, k_grant = _find_grant(grants_dir, "K",
                                  parent_public_key=root_grant["public_key"])
    # A Tier-K grant is only usable if it chains to THIS install's root and we
    # hold its private key. Stale grants from another trust root are the single
    # most confusing failure here, so name it explicitly.
    usable = bool(k_grant) and k_grant.get("parent_public_key") == root_grant["public_key"]
    if k_grant and not usable:
        print(f"      ignoring {k_path.name}: Tier-K grant does not chain to this "
              f"trust root")
        print(f"        its parent: {k_grant.get('parent_public_key')}")
        print(f"        this root:  {root_grant['public_key']}")
    if usable and not signer_key.exists():
        print(f"      ignoring {k_path.name}: no private key at {signer_key} "
              f"(hardware-issued grant?)")
        usable = False
    if usable:
        print(f"      already present: {k_grant['device_id']}  ({k_path})", flush=True)
    else:
        rc = _run([str(GOV / "issue_signer_key.py"),
                   "--device-id", args.device_id,
                   "--key-path", str(signer_key),
                   "--grants-dir", str(grants_dir)] + (["--rotate"] if signer_key.exists() else []),
                  env)
        if rc != 0:
            print("\nERROR: could not issue a Tier-K signer.", file=sys.stderr)
            return rc
        k_path = grants_dir / f"{args.device_id}.json"
        k_grant = json.loads(k_path.read_text())

    # ── 3. Admission ceremony ──────────────────────────────────────────
    _step(3, TOTAL, "Admission ceremony")
    ceremony_dir.mkdir(parents=True, exist_ok=True)
    rc = _run([str(GOV / "run_admission_ceremony.py"), str(active_set),
               "--output", str(ceremony_dir), "--operator", args.operator], env)
    if rc != 0:
        print("\nERROR: the admission ceremony failed. The packs are not internally",
              file=sys.stderr)
        print("consistent, so there is nothing safe to sign yet. Fix what it reported",
              file=sys.stderr)
        print("above, then re-run this command.", file=sys.stderr)
        return rc

    # ── 4. Sign ────────────────────────────────────────────────────────
    _step(4, TOTAL, "Signature")
    from core.spine.active_set_signature import signed_artifact_path
    signed_path = signed_artifact_path(active_set)
    rc = _run([str(GOV / "sign_active_set.py"), str(active_set),
               "--ceremony-dir", str(ceremony_dir),
               "--signer-grant", str(k_path),
               "--signer-key", str(signer_key)], env)
    if rc != 0:
        print("\nERROR: signing failed.", file=sys.stderr)
        return rc

    # ── 5. Verify exactly as the boot gate will ────────────────────────
    _step(5, TOTAL, "Verify (same check the server runs at boot)")
    from core.spine.active_set_signature import (
        SIG_OK, make_grant_lookup_from_dir, verify_active_set_signature,
    )
    trusted_keys = Path(os.environ.get("ENKI_TRUSTED_KEYS_PATH", "/etc/enki/trusted_keys.signed"))
    ok, code, ctx = verify_active_set_signature(
        active_set_path=active_set,
        signed_path=signed_path,
        trusted_keys_path=trusted_keys,
        grant_lookup=make_grant_lookup_from_dir(grants_dir),
    )
    if not ok:
        print(f"      FAILED: {code}")
        print(f"      {ctx}")
        print("\nThe signature was written but does not verify, so the server would")
        print("refuse to boot on a profile that requires it. Most often this means")
        print(f"{trusted_keys} does not list this install's Tier-R key.", file=sys.stderr)
        return 1

    print(f"      OK — signer={ctx.get('signer_device_id')} signed_at={ctx.get('signed_at')}")
    print("\n" + "=" * 66)
    print("Governance is set up.")
    print("=" * 66)
    print(f"  active set:  {active_set}")
    print(f"  signature:   {signed_path}")
    print(f"  trust root:  {root_grant['device_id']}")
    print(f"  signer:      {k_grant['device_id']} (Tier K, software key)")
    print()
    print("  Re-run this command after changing any pack — it will re-run the")
    print("  ceremony and re-sign. Any profile can now boot, including the ones")
    print("  that require a signed active set.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
