"""File browser — queries file index signals to build a browseable tree.

Also handles ingest commands (tell agent to read and upload specific files)
and move commands (tell agent to physically relocate files).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from storage.postgres.db import get_connection

log = logging.getLogger(__name__)


def get_file_tree(
    tenant_id: str,
    device_id: str,
    dsn: Optional[str] = None,
) -> dict:
    """Get the latest file index for a device and build a browseable tree summary."""
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Get the most recent file_index signal
            cur.execute("""
                SELECT payload, timestamp
                FROM signals
                WHERE tenant_id = %s AND device_id = %s
                  AND signal_type = 'signal.device.file_index'
                ORDER BY timestamp DESC
                LIMIT 1
            """, (tenant_id, device_id))
            row = cur.fetchone()
            if not row:
                return {"indexed": False, "message": "No file index yet — waiting for first scan"}

            payload = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
            scanned_at = row[1].isoformat() if row[1] else None

            # Also get the latest file_change signal for recent changes
            cur.execute("""
                SELECT payload, timestamp
                FROM signals
                WHERE tenant_id = %s AND device_id = %s
                  AND signal_type = 'signal.device.file_change'
                ORDER BY timestamp DESC
                LIMIT 1
            """, (tenant_id, device_id))
            change_row = cur.fetchone()
            changes = {}
            if change_row:
                changes = change_row[0] if isinstance(change_row[0], dict) else json.loads(change_row[0]) if change_row[0] else {}

            return {
                "indexed": True,
                "scanned_at": scanned_at,
                "total_files": payload.get("total_files", 0),
                "total_size_gb": payload.get("total_size_gb", 0),
                "scan_number": payload.get("scan_number", 0),
                "directories": payload.get("directories", []),
                "extensions": payload.get("extensions", []),
                "scan_roots": payload.get("scan_roots", []),
                "changes": {
                    "new_count": changes.get("new_count", 0),
                    "modified_count": changes.get("modified_count", 0),
                    "deleted_count": changes.get("deleted_count", 0),
                    "new_files": changes.get("new_files", [])[:20],
                    "deleted_files": changes.get("deleted_files", [])[:20],
                },
            }


def get_files_by_extension(
    tenant_id: str,
    device_id: str,
    extension: str,
    dsn: Optional[str] = None,
) -> list[dict]:
    """Get all files of a specific extension from the latest file_change signals.

    This searches through file_change signals that contain new_files lists.
    For a full file listing, the agent needs to send a detailed index.
    """
    with get_connection(dsn) as conn:
        with conn.cursor() as cur:
            # Search through file_index and file_change payloads
            cur.execute("""
                SELECT payload
                FROM signals
                WHERE tenant_id = %s AND device_id = %s
                  AND signal_type IN ('signal.device.file_index', 'signal.device.file_change')
                ORDER BY timestamp DESC
                LIMIT 10
            """, (tenant_id, device_id))

            files = []
            seen_paths = set()
            for row in cur.fetchall():
                payload = row[0] if isinstance(row[0], dict) else json.loads(row[0]) if row[0] else {}
                for f in payload.get("new_files", []):
                    if f.get("ext", "").lower() == extension.lower() and f["path"] not in seen_paths:
                        files.append(f)
                        seen_paths.add(f["path"])
            return files


def queue_ingest_command(
    tenant_id: str,
    device_id: str,
    file_paths: list[str],
    dsn: Optional[str] = None,
) -> dict:
    """Queue a command for the agent to read and upload specific files."""
    from storage.signals.device_pairing import queue_command
    return queue_command(
        device_id=device_id,
        tenant_id=tenant_id,
        command_type="ingest_files",
        params={"paths": file_paths, "wifi_only": True},
    )


def queue_move_command(
    tenant_id: str,
    device_id: str,
    moves: list[dict],  # [{"from": "path", "to": "path"}, ...]
    dsn: Optional[str] = None,
) -> dict:
    """Queue a command for the agent to physically move/rename files."""
    from storage.signals.device_pairing import queue_command
    return queue_command(
        device_id=device_id,
        tenant_id=tenant_id,
        command_type="move_files",
        params={"moves": moves},
    )


def queue_organize_command(
    tenant_id: str,
    device_id: str,
    plan: dict,  # {"folders": [{"name": "Tax Docs", "files": ["path1", "path2"]}]}
    dsn: Optional[str] = None,
) -> dict:
    """Queue an organization plan — creates folders and moves files."""
    from storage.signals.device_pairing import queue_command
    return queue_command(
        device_id=device_id,
        tenant_id=tenant_id,
        command_type="organize_files",
        params={"plan": plan},
    )
