"""The README says every pinned pack hash can be recomputed from the files. Check it."""
import json

import pytest

from core.spine.content_hash import compute_pack_hash, validate_hash_format
from conftest import REPO_ROOT

ACTIVE_SET = json.loads((REPO_ROOT / "active_set_v6.json").read_text(encoding="utf-8"))
PINS = ACTIVE_SET["packs"]


def test_active_set_pins_packs():
    assert PINS, "active set pins no packs"
    ids = [p["pack_id"] for p in PINS]
    assert len(ids) == len(set(ids)), "a pack is pinned twice"


@pytest.mark.parametrize("pin", PINS, ids=[p["pack_id"] for p in PINS])
def test_pinned_hash_reproduces_from_the_files(pin):
    assert validate_hash_format(pin["content_hash"])
    pack_dir = REPO_ROOT / "libraries" / pin["pack_id"]
    assert pack_dir.is_dir(), f"pinned pack {pin['pack_id']} has no directory"
    assert compute_pack_hash(str(pack_dir)) == pin["content_hash"]


POLICY_PINS = ACTIVE_SET["metadata"]["policy_packs"]


def test_every_policy_pack_is_pinned():
    shipped = {d.name for d in (REPO_ROOT / "libraries").iterdir()
               if (d / "pack.json").exists() and json.loads((d / "pack.json").read_text(encoding="utf-8")).get("type") == "policy"}
    assert shipped, "no policy packs found"
    assert shipped == set(POLICY_PINS), "a policy pack is shipped without a pin, or pinned without being shipped"


@pytest.mark.parametrize("pack_id", sorted(POLICY_PINS))
def test_pinned_policy_hash_reproduces_from_the_files(pack_id):
    pin = POLICY_PINS[pack_id]
    assert validate_hash_format(pin["content_hash"])
    pack_dir = REPO_ROOT / "libraries" / pack_id
    assert json.loads((pack_dir / "pack.json").read_text(encoding="utf-8"))["version"] == pin["version"]
    assert compute_pack_hash(str(pack_dir)) == pin["content_hash"]
