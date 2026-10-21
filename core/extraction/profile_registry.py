"""Extraction profile registry — assigns a stable profile_id to every
(model, prompt, version) combination so federation peers can route work.

Profile semantics:
    profile_id   = friendly slug for human queries (e.g. 'gemma3_4b__entity_extraction_v1__v1')
    fingerprint  = sha256 hash of the ingredients (model_digest + prompt + version + temp +
                   ollama_version). Two nodes with the same fingerprint produce
                   byte-identical extractions on the same input — the federation
                   subscriber uses this to decide whether to trust a peer's envelope.

Why a registry table (extraction_profiles, migration 074):
    - Replay: we can rerun "every doc tagged with profile X" after a model upgrade
    - Federation: peers advertise which profiles they can produce, hub routes work
    - Audit: every extraction row carries the profile_id so we know what produced it

Schema (migration 074):
    extraction_profiles(profile_id PK, model_name, model_digest, ollama_version,
                        prompt_template, prompt_version, temperature, fingerprint UNIQUE,
                        capabilities[], notes, created_at, deprecated_at)

The seed row 'legacy_v1' covers all pre-profile-aware extractions (NULL
columns were backfilled to 'legacy_v1' in migration 074).
"""
from __future__ import annotations

import hashlib
import logging
import threading
from typing import Optional

log = logging.getLogger("enki.extraction.profile_registry")

# Cache so we don't roundtrip to the DB on every paragraph
_CACHE: dict[str, str] = {}
_CACHE_LOCK = threading.Lock()

# The label for rows that were written before their producer was recorded.
# It asserts one thing only: we do not know what produced these. It is NOT
# a stand-in for the current model — stamping them with today's model would
# manufacture provenance and silently corrupt every later comparison that
# filters on profile. Its fingerprint is derived from 'unrecorded', so it
# can never collide with a real regime and no peer can ever match it.
PRE_STAMP_PROFILE_ID = "unrecorded__statements__pre_stamp"


def compute_fingerprint(
    *,
    model_name: str,
    model_digest: Optional[str] = None,
    ollama_version: Optional[str] = None,
    prompt_template: str,
    prompt_version: str,
    temperature: float,
) -> str:
    """Deterministic fingerprint for a (model, prompt) combination. Two
    nodes computing this with the same inputs get the same hash, which
    is what the federation subscriber uses to know if a peer's
    extraction would match its own."""
    parts = [
        model_name or "",
        model_digest or "",
        ollama_version or "",
        prompt_template or "",
        prompt_version or "",
        f"{float(temperature):.3f}",
    ]
    src = "||".join(parts)
    return hashlib.sha256(src.encode()).hexdigest()


def prompt_version_for(prompt_text: str) -> str:
    """Content-derived prompt version: the first 12 hex of sha256(prompt).

    A hand-maintained version string only records a regime change when
    somebody remembers to bump it, and the one time it is forgotten two
    different extraction regimes share a profile_id and become permanently
    indistinguishable. Deriving the version from the prompt text makes the
    bump automatic and the identity content-derived, which is the same rule
    the sense addresses follow: identity is content, never a count.
    """
    return "p" + hashlib.sha256((prompt_text or "").encode()).hexdigest()[:12]


def _profile_id_for(model_name: str, prompt_template: str, prompt_version: str) -> str:
    """Human-friendly slug. Stable for the same inputs."""
    safe_model = (model_name or "unknown").replace(":", "_").replace("/", "_").replace(" ", "_")
    safe_prompt = (prompt_template or "default").replace(" ", "_")
    return f"{safe_model}__{safe_prompt}__{prompt_version or 'v1'}"


def get_or_create_profile(
    *,
    model_name: str,
    prompt_template: str = "entity_extraction_v1",
    prompt_version: str = "v1",
    temperature: float = 0.0,
    model_digest: Optional[str] = None,
    ollama_version: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
    notes: Optional[str] = None,
) -> str:
    """Get the profile_id for this (model, prompt) combination, creating
    a registry row on first use. Returns the profile_id (TEXT) suitable
    for stamping on extraction rows.

    Cached per-process — typical cost is one DB roundtrip per (model,
    prompt) pair per worker restart.

    Idempotent. Never raises — falls back to 'legacy_v1' on DB error
    so extraction never blocks on a profile registration issue.
    """
    profile_id = _profile_id_for(model_name, prompt_template, prompt_version)

    with _CACHE_LOCK:
        if profile_id in _CACHE:
            return _CACHE[profile_id]

    try:
        from storage.postgres.db import get_connection
        fingerprint = compute_fingerprint(
            model_name=model_name, model_digest=model_digest,
            ollama_version=ollama_version, prompt_template=prompt_template,
            prompt_version=prompt_version, temperature=temperature,
        )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO extraction_profiles (
                        profile_id, model_name, model_digest, ollama_version,
                        prompt_template, prompt_version, temperature,
                        fingerprint, capabilities, notes
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (profile_id) DO NOTHING
                    """,
                    (
                        profile_id, model_name, model_digest, ollama_version,
                        prompt_template, prompt_version, temperature,
                        fingerprint,
                        capabilities or ['entity_extraction'],
                        notes or f"Auto-registered on first use",
                    ),
                )
                conn.commit()
    except Exception as e:
        log.warning("get_or_create_profile fallback to legacy_v1 (%s)", e)
        with _CACHE_LOCK:
            _CACHE[profile_id] = "legacy_v1"
        return "legacy_v1"

    with _CACHE_LOCK:
        _CACHE[profile_id] = profile_id
    return profile_id


def register_pre_stamp_profile(cur) -> str:
    """Register the 'we do not know what produced this' profile and return
    its id. Idempotent; takes an open cursor so a backfill can register and
    stamp inside one transaction.

    Deliberately NOT reusing the seeded 'legacy_v1' row: that row claims
    model_name='gemma3:4b', which is a specific and probably wrong claim
    about statements written across several model changes.
    """
    cur.execute(
        """
        INSERT INTO extraction_profiles (
            profile_id, model_name, model_digest, ollama_version,
            prompt_template, prompt_version, temperature,
            fingerprint, capabilities, notes
        ) VALUES (%s, 'unrecorded', NULL, NULL,
                  'unrecorded', 'unrecorded', 0,
                  %s, %s, %s)
        ON CONFLICT (profile_id) DO NOTHING
        """,
        (
            PRE_STAMP_PROFILE_ID,
            compute_fingerprint(
                model_name="unrecorded", prompt_template="unrecorded",
                prompt_version="unrecorded", temperature=0.0,
            ),
            ["statement_extraction"],
            "Statements written before the batched extractor stamped a "
            "profile. Model and prompt are not recoverable per row. Not "
            "reproducible by any node, including this one — treat as "
            "evidence, never as a comparison baseline.",
        ),
    )
    return PRE_STAMP_PROFILE_ID


def derive_profile_from_llm(llm, prompt_template: str = "entity_extraction_v1",
                            prompt_version: str = "v1") -> str:
    """Convenience: extract model info from an LLM provider object and
    register/lookup the profile. Returns the profile_id.

    Falls back to 'legacy_v1' if the LLM doesn't expose a model name
    (e.g. mock objects in tests)."""
    if llm is None:
        return "legacy_v1"
    model_name = getattr(llm, "model", None) or getattr(llm, "model_name", None)
    if not model_name:
        return "legacy_v1"
    return get_or_create_profile(
        model_name=model_name,
        prompt_template=prompt_template,
        prompt_version=prompt_version,
    )
