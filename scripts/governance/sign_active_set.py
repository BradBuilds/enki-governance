#!/usr/bin/env python3
"""
Sign an active_set with a Tier-K key.

Run AFTER the admission ceremony has produced validation_transcript.json
in the ceremony output directory. The signature commits to:
  - the exact bytes of the active_set file
  - the transcript hash from the ceremony

Either changes ⇒ signature invalid.

Two signer options. Both emit the same Ed25519 signature and the boot-time
verifier cannot tell them apart — the difference is where the key lives.

  SOFTWARE (default; no hardware needed):
    python scripts/governance/sign_active_set.py \\
        active_set_v6.json \\
        --ceremony-dir storage/admission/v1/ \\
        --signer-grant storage/grants/local-signer.json \\
        --signer-key storage/keys/signer_key.enc

  HARDWARE (HWKEY over USB; for hardened/evidentiary installs):
    python scripts/governance/sign_active_set.py \\
        active_set_v6.json \\
        --ceremony-dir storage/admission/v1/ \\
        --signer-grant storage/grants/node1-HWKEY.json \\
        --device /dev/ttyACM0

Most people want `scripts/governance/setup_governance.py`, which runs the whole
chain (trust root → signer → ceremony → sign → verify) in one command.

Environment:
    ENKI_SIGNER_KEY_PATH      fallback for --signer-key
    ENKI_SIGNER_PASSWORD      unseals the software signer key
                              (falls back to ENKI_ROOT_PASSWORD)
    ENKI_HARDWARE_KEY_PATH    fallback for --device
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

# Project root on path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

# Hardware key repo on path (sibling checkout; fall back to env override)
_HW_KEY_REPO = Path(
    os.environ.get(
        "ENKI_HARDWARE_KEY_REPO",
        str(_PROJECT_ROOT.parent / "enki-hardware-key"),
    )
)
if _HW_KEY_REPO.exists():
    sys.path.insert(0, str(_HW_KEY_REPO))

from core.spine.active_set_signature import (  # noqa: E402
    build_canonical_message,
    build_signed_artifact,
    compute_active_set_file_hash,
    save_signed_active_set,
    signed_artifact_path,
)


from dataclasses import dataclass


@dataclass
class _SignerGrantView:
    """Minimal view of a DeviceGrant — just the fields the signer needs.
    Avoids pulling in storage.auth.grant (and its transitive deps) on the
    host side; the verifier inside the container uses the full DeviceGrant.
    """
    device_id: str
    device_tier: str
    public_key: str
    grant_id: str


def _load_grant(path: Path) -> _SignerGrantView:
    data = json.loads(path.read_text(encoding="utf-8"))
    return _SignerGrantView(
        device_id=data["device_id"],
        device_tier=data["device_tier"],
        public_key=data["public_key"],
        grant_id=data["grant_id"],
    )


def _load_transcript_hash(ceremony_dir: Path) -> str:
    """Load the activation_record.json that the ceremony produced and
    return its transcript_hash. We use the activation_record (not the
    transcript itself) because the ceremony has already canonicalized
    and committed to that hash."""
    record_path = ceremony_dir / "activation_record.json"
    if not record_path.exists():
        raise FileNotFoundError(
            f"activation_record.json not found in {ceremony_dir}. "
            f"Run scripts/governance/run_admission_ceremony.py first."
        )
    data = json.loads(record_path.read_text(encoding="utf-8"))
    transcript_hash = data.get("activation", {}).get("transcript_hash")
    if not transcript_hash:
        raise ValueError(f"activation_record.json missing transcript_hash: {record_path}")
    return transcript_hash


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign an active_set with a Tier-K key (software by default, hardware HWKEY optional).",
    )
    parser.add_argument("active_set", type=Path,
                        help="Path to the active_set JSON file to sign")
    parser.add_argument("--ceremony-dir", type=Path, required=True,
                        help="Directory containing activation_record.json from "
                             "run_admission_ceremony.py")
    parser.add_argument("--signer-grant", type=Path, required=True,
                        help="Path to the Tier-K signer's DeviceGrant JSON")
    parser.add_argument("--signer-key", type=Path,
                        default=os.environ.get("ENKI_SIGNER_KEY_PATH"),
                        help="Path to a password-sealed software signer key "
                             "(from issue_signer_key.py). Default software path; "
                             "no hardware required.")
    parser.add_argument("--device",
                        default=os.environ.get("ENKI_HARDWARE_KEY_PATH"),
                        help="Serial port for a HWKEY hardware key (e.g. /dev/ttyACM0). "
                             "Only used when --signer-key is not given.")
    parser.add_argument("--output", type=Path,
                        help="Output path (default: <active_set>.signed.json)")
    args = parser.parse_args()

    # ── 1. Inputs ──────────────────────────────────────────────────────
    if not args.active_set.exists():
        print(f"ERROR: active_set not found: {args.active_set}", file=sys.stderr)
        return 1
    if not args.signer_grant.exists():
        print(f"ERROR: signer grant not found: {args.signer_grant}", file=sys.stderr)
        return 1

    signer_grant = _load_grant(args.signer_grant)
    if signer_grant.device_tier != "K":
        print(f"ERROR: signer grant is Tier {signer_grant.device_tier}, "
              f"need Tier K to sign active_set.", file=sys.stderr)
        return 1

    transcript_hash = _load_transcript_hash(args.ceremony_dir)
    active_set_sha256 = compute_active_set_file_hash(args.active_set)
    canonical_message = build_canonical_message(active_set_sha256, transcript_hash)

    mode = "software key" if args.signer_key else "hardware key (HWKEY)"
    print()
    print("  ┌────────────────────────────────────────────────────────┐")
    print(f"  │  ACTIVE SET SIGNATURE — Tier K, {mode:<22}│")
    print("  └────────────────────────────────────────────────────────┘")
    print()
    print(f"  Active set:        {args.active_set}")
    print(f"  Active set sha256: {active_set_sha256}")
    print(f"  Transcript hash:   {transcript_hash}")
    print(f"  Signer device_id:  {signer_grant.device_id}")
    print(f"  Signer pubkey:     {signer_grant.public_key[:32]}...")
    print(f"  Signer grant_id:   {signer_grant.grant_id}")
    print()
    print("  Message to sign (preview, first 80 bytes):")
    print(f"    {canonical_message[:80]!r}")
    print()

    # ── 2. Produce the signature ───────────────────────────────────────
    # Two interchangeable signers. Both expose .sign(bytes) -> bytes and both
    # produce an Ed25519 signature the boot-time verifier treats identically;
    # what differs is where the private key lives.
    if args.signer_key:
        # Software path — the default, and the only one a self-hoster needs.
        key_path = Path(args.signer_key)
        if not key_path.exists():
            print(f"ERROR: signer key not found: {key_path}", file=sys.stderr)
            print("  Create one with:  python scripts/governance/issue_signer_key.py",
                  file=sys.stderr)
            return 1
        password = os.environ.get("ENKI_SIGNER_PASSWORD") or os.environ.get("ENKI_ROOT_PASSWORD")
        if not password:
            print("ERROR: set ENKI_SIGNER_PASSWORD (or ENKI_ROOT_PASSWORD) to unseal "
                  f"{key_path}.", file=sys.stderr)
            return 1
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from storage.auth.root_key import load_signer_key
        try:
            signer = load_signer_key(str(key_path), password)
        except Exception as e:
            print(f"ERROR: could not unseal signer key {key_path}: {e}", file=sys.stderr)
            print("  Check ENKI_SIGNER_PASSWORD.", file=sys.stderr)
            return 1
        # Refuse to emit a signature the verifier could never accept.
        if signer.public_key_base64() != signer_grant.public_key:
            print("ERROR: this key does not match the signer grant.", file=sys.stderr)
            print(f"  key   pubkey: {signer.public_key_base64()}", file=sys.stderr)
            print(f"  grant pubkey: {signer_grant.public_key}", file=sys.stderr)
            return 1
        print(f"  Signer key: {key_path} (software)")
        print()
        signature = signer.sign(canonical_message)
    else:
        # Hardware path — still supported, now opt-in.
        try:
            from server.serial_transport import detect_hardware_key  # type: ignore
            from server.hardware_key import HardwareKeyAdapter  # type: ignore
        except ImportError as e:
            print(f"ERROR: cannot import enki-hardware-key modules: {e}", file=sys.stderr)
            print("  Either set ENKI_HARDWARE_KEY_REPO to the repo checkout, or use a",
                  file=sys.stderr)
            print("  software key instead (no hardware needed):", file=sys.stderr)
            print("    python scripts/governance/issue_signer_key.py", file=sys.stderr)
            print("    ... then re-run this with --signer-key storage/keys/signer_key.enc",
                  file=sys.stderr)
            return 1

        port = args.device or detect_hardware_key(expected_device_id=signer_grant.device_id)
        if not port:
            print("ERROR: no HWKEY detected on USB. Plug it in and try again.",
                  file=sys.stderr)
            return 1

        print(f"  HWKEY port: {port}")
        print()
        print("  >>> Approve the signing request on the watch. <<<")
        print()

        adapter = HardwareKeyAdapter(port, public_key_b64=signer_grant.public_key)
        try:
            signature = adapter.sign(canonical_message)
        finally:
            adapter.close()

    sig_b64 = base64.b64encode(signature).decode("ascii")

    # ── 3. Write the signed artifact ───────────────────────────────────
    out_path = args.output or signed_artifact_path(args.active_set)
    signed = build_signed_artifact(
        active_set_path=args.active_set,
        transcript_hash=transcript_hash,
        signer_grant=signer_grant,
        signature_b64=sig_b64,
    )
    save_signed_active_set(signed, out_path)

    print(f"  Signature: {sig_b64[:32]}...")
    print(f"  Signed at: {signed.signed_at}")
    print(f"  Written:   {out_path}")
    print()
    print("  Done. Profiles that require a signed active set (node, node-public,")
    print("  evidentiary, cloud, hardware, hardened) will now verify this at boot.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
