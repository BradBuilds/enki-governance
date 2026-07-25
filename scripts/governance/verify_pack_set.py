#!/usr/bin/env python3
"""
Pack Set Verifier — dry-run validation for an active set.

Runs every check the admission ceremony performs, but writes NO artifacts.
Use this to validate a draft active set + pack collection BEFORE running
the real ceremony. A green run guarantees the activation gate will accept
the pack set; a red run tells you exactly what to fix.

Checks performed (same modules the ceremony uses):
  1. Active set JSON parses and conforms to its schema
  2. Each declared pack exists at libraries/<pack_id>
  3. Each pack.json parses and has the required fields
  4. Each definition file referenced in pack.json exists and parses
  5. Pack content hashes (RFC 8785 JCS + NFC) match the active set declaration
  6. Each pack's namespace declarations are well-formed
  7. No namespace collisions across the pack set
  8. Every definition ID in each pack belongs to one of that pack's owned namespaces
  9. Pack dependencies are all satisfied (declared dependencies exist in the active set)

Usage:
    python3 scripts/governance/verify_pack_set.py [active_set_path]

Default active set: active_set_v5.json
"""

import argparse
import json
import sys
from pathlib import Path

# Project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from core.spine.active_set import ActiveSet
from core.spine.content_hash import compute_pack_hash, verify_pack_hash
from core.spine import namespace_registry

LIBRARIES_DIR = PROJECT_ROOT / "libraries"

# ANSI color codes (degrade gracefully if not a TTY)
class C:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

if not sys.stdout.isatty():
    for k in dir(C):
        if not k.startswith("_"):
            setattr(C, k, "")


def header(text: str):
    print(f"\n{C.BOLD}{C.BLUE}{'═' * 70}{C.RESET}")
    print(f"{C.BOLD}{C.BLUE}{text}{C.RESET}")
    print(f"{C.BOLD}{C.BLUE}{'═' * 70}{C.RESET}")


def step(num: int, text: str):
    print(f"\n{C.BOLD}Step {num}:{C.RESET} {text}")


def ok(msg: str):
    print(f"  {C.GREEN}OK{C.RESET}    {msg}")


def fail(msg: str):
    print(f"  {C.RED}FAIL{C.RESET}  {msg}")


def warn(msg: str):
    print(f"  {C.YELLOW}WARN{C.RESET}  {msg}")


def detail(msg: str):
    print(f"        {C.DIM}{msg}{C.RESET}")


class Verifier:
    def __init__(self, active_set_path: Path):
        self.active_set_path = active_set_path
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.active_set: ActiveSet | None = None
        self.pack_data: dict[str, dict] = {}  # pack_id → loaded pack.json dict
        self.pack_paths: dict[str, Path] = {}

    def fail_check(self, msg: str):
        self.errors.append(msg)
        fail(msg)

    def warn_check(self, msg: str):
        self.warnings.append(msg)
        warn(msg)

    # ── Step 1
    def check_active_set_loads(self) -> bool:
        step(1, f"Load active set ({self.active_set_path.name})")
        if not self.active_set_path.exists():
            self.fail_check(f"file not found: {self.active_set_path}")
            return False
        try:
            with open(self.active_set_path) as f:
                raw = json.load(f)
            self.active_set = ActiveSet.from_dict(raw)
            ok(f"parsed — version {self.active_set.version}, "
               f"{len(self.active_set.packs)} packs declared")
            return True
        except json.JSONDecodeError as e:
            self.fail_check(f"JSON parse error: {e}")
            return False
        except KeyError as e:
            self.fail_check(f"missing required field: {e}")
            return False
        except Exception as e:
            self.fail_check(f"{type(e).__name__}: {e}")
            return False

    # ── Step 2
    def check_packs_exist(self) -> bool:
        step(2, "Pack directories exist")
        all_ok = True
        for pack in self.active_set.packs:
            pack_path = LIBRARIES_DIR / pack.pack_id
            if not pack_path.exists():
                self.fail_check(f"{pack.pack_id} — directory missing at {pack_path}")
                all_ok = False
                continue
            if not (pack_path / "pack.json").exists():
                self.fail_check(f"{pack.pack_id} — pack.json missing")
                all_ok = False
                continue
            self.pack_paths[pack.pack_id] = pack_path
            ok(f"{pack.pack_id}")
        return all_ok

    # ── Step 3
    def check_pack_manifests_parse(self) -> bool:
        step(3, "Pack manifests parse and have required fields")
        all_ok = True
        # The spine treats dependencies and namespaces_owned as optional
        # (defaulting to empty list); only the core descriptive fields are
        # truly required. v4 packs like 'language' and 'numeric' omit dependencies.
        REQUIRED_FIELDS = ["pack_id", "version", "name", "description", "definitions"]
        for pack in self.active_set.packs:
            path = self.pack_paths[pack.pack_id] / "pack.json"
            try:
                with open(path) as f:
                    data = json.load(f)
            except json.JSONDecodeError as e:
                self.fail_check(f"{pack.pack_id} — pack.json parse error: {e}")
                all_ok = False
                continue
            missing = [f for f in REQUIRED_FIELDS if f not in data]
            if missing:
                self.fail_check(f"{pack.pack_id} — missing fields: {missing}")
                all_ok = False
                continue
            if data["pack_id"] != pack.pack_id:
                self.fail_check(f"{pack.pack_id} — pack.json says pack_id="
                                f"{data['pack_id']!r}, active set says {pack.pack_id!r}")
                all_ok = False
                continue
            if data["version"] != pack.version:
                self.fail_check(f"{pack.pack_id} — version mismatch: "
                                f"pack.json={data['version']}, active set={pack.version}")
                all_ok = False
                continue
            # Normalize optional fields
            data.setdefault("dependencies", [])
            data.setdefault("namespaces_owned", [])
            self.pack_data[pack.pack_id] = data
            ok(f"{pack.pack_id} v{data['version']} — "
               f"{len(data['definitions'])} definitions, "
               f"{len(data['namespaces_owned'])} namespaces, "
               f"{len(data['dependencies'])} deps")
        return all_ok

    # ── Step 4
    def check_definition_files_exist(self) -> bool:
        step(4, "Definition files exist and parse")
        all_ok = True
        for pack_id, data in self.pack_data.items():
            pack_path = self.pack_paths[pack_id]
            for defn in data["definitions"]:
                rel_path = defn["path"]
                file_path = pack_path / rel_path
                if not file_path.exists():
                    self.fail_check(f"{pack_id} — missing definition file: {rel_path}")
                    all_ok = False
                    continue
                if file_path.suffix == ".json":
                    try:
                        with open(file_path) as f:
                            json.load(f)
                    except json.JSONDecodeError as e:
                        self.fail_check(f"{pack_id} — {rel_path} JSON parse error: {e}")
                        all_ok = False
                        continue
                ok(f"{pack_id} → {rel_path}")
        return all_ok

    # ── Step 5
    def check_content_hashes(self) -> bool:
        step(5, "Content hashes (RFC 8785 JCS + NFC SHA-256)")
        all_ok = True
        for pack in self.active_set.packs:
            pack_path = self.pack_paths[pack.pack_id]
            try:
                computed = compute_pack_hash(str(pack_path))
            except Exception as e:
                self.fail_check(f"{pack.pack_id} — hash computation failed: {e}")
                all_ok = False
                continue
            if pack.content_hash is None:
                self.warn_check(f"{pack.pack_id} — no content_hash declared "
                                f"(would be {computed})")
                continue
            if computed == pack.content_hash:
                ok(f"{pack.pack_id} {pack.content_hash}")
            else:
                self.fail_check(f"{pack.pack_id} hash mismatch")
                detail(f"declared: {pack.content_hash}")
                detail(f"computed: {computed}")
                all_ok = False
        return all_ok

    # ── Step 6
    def check_namespace_declarations(self) -> bool:
        step(6, "Namespace declarations are well-formed")
        all_ok = True
        for pack_id, data in self.pack_data.items():
            namespaces = data.get("namespaces_owned", [])
            errors = namespace_registry.validate_namespace_declarations(pack_id, namespaces)
            if errors:
                for e in errors:
                    self.fail_check(f"{pack_id} — {e.code}: {e.message}")
                    detail(f"namespace: {e.context.get('namespace')}")
                    detail(f"pattern:   {e.context.get('pattern')}")
                all_ok = False
            else:
                ok(f"{pack_id} — owns {namespaces}")
        return all_ok

    # ── Step 7
    def check_namespace_collisions(self) -> bool:
        step(7, "No namespace collisions across pack set")
        packs_namespaces = {
            pid: data.get("namespaces_owned", [])
            for pid, data in self.pack_data.items()
        }
        errors = namespace_registry.detect_namespace_collisions(packs_namespaces)
        if errors:
            for e in errors:
                self.fail_check(f"{e.code}: {e.message}")
                detail(f"pack A: {e.context.get('pack_id')} owns {e.context.get('namespace')}")
                detail(f"pack B: {e.context.get('conflicting_pack_id')} owns "
                       f"{e.context.get('conflicting_namespace')}")
            return False
        ok(f"no collisions across {len(packs_namespaces)} packs")
        return True

    # ── Step 8
    def check_definition_ownership(self) -> bool:
        step(8, "Every definition ID belongs to its pack's owned namespaces")
        all_ok = True
        for pack_id, data in self.pack_data.items():
            namespaces = data.get("namespaces_owned", [])
            def_ids = [d["id"] for d in data["definitions"]]
            errors = namespace_registry.verify_definition_ownership(
                pack_id=pack_id,
                namespaces_owned=namespaces,
                definition_ids=def_ids,
            )
            if errors:
                for e in errors:
                    self.fail_check(f"{pack_id} — {e.code}: {e.message}")
                    detail(f"definition: {e.context.get('definition_id')}")
                    detail(f"owned: {e.context.get('owned_namespaces')}")
                all_ok = False
            else:
                ok(f"{pack_id} — all {len(def_ids)} definitions in owned namespaces")
        return all_ok

    # ── Step 9
    def check_dependencies(self) -> bool:
        step(9, "Pack dependencies satisfied")
        declared_packs = {p.pack_id for p in self.active_set.packs}
        all_ok = True
        for pack_id, data in self.pack_data.items():
            deps = data.get("dependencies", [])
            missing = [d for d in deps if d not in declared_packs]
            if missing:
                self.fail_check(f"{pack_id} — depends on packs not in active set: {missing}")
                all_ok = False
            else:
                ok(f"{pack_id} — deps satisfied: {deps}" if deps else f"{pack_id} — no deps")
        return all_ok

    # ── Run all
    def run(self) -> int:
        header(f"Pack Set Verifier — {self.active_set_path.name}")
        steps = [
            self.check_active_set_loads,
            self.check_packs_exist,
            self.check_pack_manifests_parse,
            self.check_definition_files_exist,
            self.check_content_hashes,
            self.check_namespace_declarations,
            self.check_namespace_collisions,
            self.check_definition_ownership,
            self.check_dependencies,
        ]
        for fn in steps:
            if not fn():
                if fn in (self.check_active_set_loads,
                          self.check_packs_exist,
                          self.check_pack_manifests_parse):
                    print(f"\n{C.RED}{C.BOLD}STOPPING{C.RESET} — cannot continue without "
                          f"valid pack manifests")
                    break

        # Summary
        header("SUMMARY")
        if self.errors:
            print(f"{C.RED}{C.BOLD}{len(self.errors)} error(s):{C.RESET}")
            for e in self.errors:
                print(f"  {C.RED}•{C.RESET} {e}")
        if self.warnings:
            print(f"{C.YELLOW}{C.BOLD}{len(self.warnings)} warning(s):{C.RESET}")
            for w in self.warnings:
                print(f"  {C.YELLOW}•{C.RESET} {w}")
        if not self.errors:
            print(f"\n{C.GREEN}{C.BOLD}READY TO ACTIVATE{C.RESET} — all checks passed")
            print(f"{C.DIM}Next: run scripts/governance/run_admission_ceremony.py "
                  f"{self.active_set_path.name}{C.RESET}")
            return 0
        else:
            print(f"\n{C.RED}{C.BOLD}NOT READY{C.RESET} — fix errors above before activating")
            return 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("active_set", nargs="?", default="active_set_v5.json",
                    help="path to active set JSON (default: active_set_v5.json)")
    args = ap.parse_args()

    active_set_path = Path(args.active_set)
    if not active_set_path.is_absolute():
        active_set_path = PROJECT_ROOT / active_set_path

    return Verifier(active_set_path).run()


if __name__ == "__main__":
    sys.exit(main())
