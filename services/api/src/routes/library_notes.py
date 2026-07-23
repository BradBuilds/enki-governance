"""
Library notes HTTP API.

Phase 6 of the Library Federation plan
(the internal library federation plan).

Endpoints:
  POST /libraries/{library_id}/notes        — create a note (append-only)
  GET  /libraries/{library_id}/notes        — list notes for a library
  POST /libraries/{library_id}/notes/{sha}/delete  — soft-delete (creates a deletion marker)

Federation sync happens automatically: notes appear in this library's
manifest v2 notes[] section, and subscribers consume them via
library_subscriber._apply_v2_bundle_sections.
"""
from __future__ import annotations

import logging
import os
import socket
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

log = logging.getLogger("enki.routes.library_notes")
router = APIRouter(tags=["library-notes"])


def _node_id() -> str:
    """Stable peer identifier for note authorship."""
    return (os.environ.get("ENKI_NODE_ID") or f"node:{socket.gethostname()}").strip()


# ── Request models ────────────────────────────────────────────────────────


class CreateNoteReq(BaseModel):
    subject_kind: str = Field(description="entity | document | mention | statement | assessment | library")
    subject_ref:  Optional[str] = None
    body:         str = Field(min_length=1, max_length=200000)
    body_format:  str = "markdown"
    visibility:   str = "library"
    parent_note_sha: Optional[str] = None
    author_user_id:  Optional[str] = None
    author_display_name: Optional[str] = None


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/libraries/{library_id}/notes")
def create_library_note(library_id: int, req: CreateNoteReq, request: Request):
    """Create a note attached to a library subject. Returns the new
    note row including its note_sha256. Idempotent: posting identical
    content with the same created_at returns the existing row."""
    from core.federation.notes import create_note

    # Resolve author identity. Prefer authenticated user info if the
    # request carries it; fall back to the operator node identity.
    author_user_id = req.author_user_id
    author_display_name = req.author_display_name
    try:
        from services.api.src.unified_auth import optional_unified_auth, SubjectType
        auth = None
        # Can't await here; only sync info from headers if available
        # via the standard Depends path. For now, accept what client sent.
        # (Production hardening: make this Depends(unified_auth) so the
        # author is always cryptographically attributed.)
    except Exception:
        pass

    try:
        note = create_note(
            library_id=library_id,
            subject_kind=req.subject_kind,
            subject_ref=req.subject_ref,
            body=req.body,
            author_node_id=_node_id(),
            author_user_id=author_user_id,
            author_display_name=author_display_name,
            body_format=req.body_format,
            visibility=req.visibility,
            parent_note_sha=req.parent_note_sha,
        )
        return note
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("create_library_note failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])


@router.get("/libraries/{library_id}/notes")
def list_library_notes(library_id: int, include_deleted: bool = False):
    """List notes for a library, newest first. include_deleted=true
    shows the full audit trail (including soft-deleted entries)."""
    from core.federation.notes import list_notes_for_library
    notes = list_notes_for_library(library_id, include_deleted=include_deleted)
    return {"library_id": library_id, "notes": notes, "count": len(notes)}


@router.post("/libraries/{library_id}/notes/{note_sha256}/delete")
def soft_delete_note(library_id: int, note_sha256: str):
    """Soft-delete: creates a new note with is_deletion_marker=TRUE
    whose parent_note_sha points at the target. Original row is
    preserved for audit."""
    from core.federation.notes import create_note

    try:
        deletion = create_note(
            library_id=library_id,
            subject_kind="library",
            subject_ref=None,
            body=f"[deleted note {note_sha256[:16]}]",
            author_node_id=_node_id(),
            visibility="library",
            parent_note_sha=note_sha256,
            is_deletion_marker=True,
        )
        return deletion
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        log.exception("soft_delete_note failed")
        raise HTTPException(status_code=500, detail=str(e)[:300])
