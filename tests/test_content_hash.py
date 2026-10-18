import json
import shutil
import unicodedata

import pytest

from core.spine.content_hash import (
    compute_pack_hash,
    compute_transcript_hash,
    normalize_json_bytes,
    validate_hash_format,
    verify_pack_hash,
)


def test_key_order_does_not_change_the_bytes():
    assert normalize_json_bytes({"b": 1, "a": [2, {"d": 4, "c": 3}]}) == normalize_json_bytes({"a": [2, {"c": 3, "d": 4}], "b": 1})


def test_canonical_form_has_sorted_keys_and_no_whitespace():
    assert normalize_json_bytes({"b": 1, "a": "x"}) == b'{"a":"x","b":1}'


def test_unicode_is_nfc_normalised():
    composed = unicodedata.normalize("NFC", "café")
    decomposed = unicodedata.normalize("NFD", "café")
    assert composed != decomposed
    assert normalize_json_bytes({"name": composed}) == normalize_json_bytes({"name": decomposed})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(bad):
    with pytest.raises(ValueError):
        normalize_json_bytes({"value": bad})


@pytest.mark.parametrize("value, ok", [
    ("sha256:" + "a" * 64, True),
    ("sha256:" + "A1" * 32, True),
    ("sha256:" + "a" * 63, False),
    ("sha256:" + "g" * 64, False),
    ("md5:" + "a" * 64, False),
    ("a" * 64, False),
])
def test_hash_format(value, ok):
    assert validate_hash_format(value) is ok


def test_transcript_hash_is_stable_and_order_sensitive():
    a, b = {"gate": "A", "result": "pass"}, {"gate": "B", "result": "pass"}
    assert compute_transcript_hash([a, b]) == compute_transcript_hash([dict(reversed(list(a.items()))), b])
    assert compute_transcript_hash([a, b]) != compute_transcript_hash([b, a])
    assert validate_hash_format(compute_transcript_hash([]))


@pytest.fixture
def pack(tmp_path):
    """A minimal pack laid out the way the pinned packs are: pack.json lists its definition files."""
    root = tmp_path / "samplepack"
    (root / "definitions").mkdir(parents=True)
    (root / "definitions" / "b.terms.json").write_text(json.dumps({"terms": ["beta"]}), encoding="utf-8")
    (root / "definitions" / "a.terms.json").write_text(json.dumps({"terms": ["alpha"]}), encoding="utf-8")
    (root / "definitions" / "notes.txt").write_bytes(b"raw bytes\n")
    (root / "pack.json").write_text(json.dumps({
        "id": "samplepack", "version": "0.1.0",
        "definitions": [{"path": "definitions/b.terms.json"}, {"path": "definitions/a.terms.json"},
                        {"path": "definitions/notes.txt"}],
    }), encoding="utf-8")
    return root


def test_pack_hash_is_reproducible(pack, tmp_path):
    first = compute_pack_hash(str(pack))
    assert validate_hash_format(first)
    copy = tmp_path / "elsewhere" / "samplepack"
    shutil.copytree(pack, copy)
    assert compute_pack_hash(str(copy)) == first
    assert verify_pack_hash(str(copy), first)


def test_reformatting_a_json_definition_keeps_the_hash(pack):
    before = compute_pack_hash(str(pack))
    target = pack / "definitions" / "a.terms.json"
    target.write_text(json.dumps(json.loads(target.read_text(encoding="utf-8")), indent=8), encoding="utf-8")
    assert compute_pack_hash(str(pack)) == before


def test_changing_a_definition_changes_the_hash(pack):
    before = compute_pack_hash(str(pack))
    target = pack / "definitions" / "a.terms.json"
    target.write_text(json.dumps({"terms": ["alpha", "tampered"]}), encoding="utf-8")
    assert compute_pack_hash(str(pack)) != before
    assert not verify_pack_hash(str(pack), before)


def test_non_json_definitions_are_hashed_byte_for_byte(pack):
    before = compute_pack_hash(str(pack))
    (pack / "definitions" / "notes.txt").write_bytes(b"raw bytes\r\n")
    assert compute_pack_hash(str(pack)) != before


def test_changing_the_manifest_changes_the_hash(pack):
    before = compute_pack_hash(str(pack))
    manifest = json.loads((pack / "pack.json").read_text(encoding="utf-8"))
    manifest["version"] = "0.1.1"
    (pack / "pack.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert compute_pack_hash(str(pack)) != before


def test_a_listed_definition_that_is_missing_is_an_error(pack):
    (pack / "definitions" / "a.terms.json").unlink()
    with pytest.raises(FileNotFoundError):
        compute_pack_hash(str(pack))


def _manifest_pack(root):
    """A pack laid out the way policy packs are: files listed as plain paths under "manifest"."""
    (root / "definitions").mkdir(parents=True)
    (root / "definitions" / "p.weights.json").write_text(json.dumps({"content": {"weights": {"exact": 3.0}}}), encoding="utf-8")
    (root / "pack.json").write_text(json.dumps({
        "id": "samplepolicy", "version": "0.1.0", "type": "policy",
        "manifest": ["definitions/p.weights.json"],
    }), encoding="utf-8")
    return root


def test_files_listed_under_manifest_are_part_of_the_hash(tmp_path):
    root = _manifest_pack(tmp_path / "samplepolicy")
    before = compute_pack_hash(str(root))
    (root / "definitions" / "p.weights.json").write_text(json.dumps({"content": {"weights": {"exact": 9.0}}}), encoding="utf-8")
    assert compute_pack_hash(str(root)) != before


def test_a_manifest_file_that_is_missing_is_an_error(tmp_path):
    root = _manifest_pack(tmp_path / "samplepolicy")
    (root / "definitions" / "p.weights.json").unlink()
    with pytest.raises(FileNotFoundError):
        compute_pack_hash(str(root))


def test_missing_manifest_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        compute_pack_hash(str(tmp_path))
