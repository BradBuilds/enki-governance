"""
Watch folder daemon — monitors /data/inbox/ for new files and ingests them.

Supports:
  - Individual files: JSON, CSV, email, PDF, DOCX, XLSX, HTML, images, XML
  - Archives: ZIP, TAR, MBOX (split by archive splitter, then each chunk ingested)
  - Google Takeout ZIPs: auto-detected by directory structure
  - Apple Privacy export ZIPs: same pattern

Files move to /data/processed/ on success, /data/failed/ on error.

Usage:
    start_watch_folder("default")   # starts background thread
    stop_watch_folder()             # stops it
"""

import os
import time
import shutil
import logging
import threading
from pathlib import Path

logger = logging.getLogger("enki.watchfolder")

_watcher_thread: threading.Thread | None = None
_watcher_stop = threading.Event()

# Configurable via env
INBOX_DIR = os.environ.get("ENKI_INBOX_DIR", "/data/inbox")
PROCESSED_DIR = os.environ.get("ENKI_PROCESSED_DIR", "/data/processed")
FAILED_DIR = os.environ.get("ENKI_FAILED_DIR", "/data/failed")
POLL_INTERVAL = int(os.environ.get("ENKI_WATCH_INTERVAL", "5"))  # seconds

# MIME type hints by extension
EXTENSION_MAP = {
    ".json": "application/json",
    ".csv": "text/csv",
    ".xml": "application/xml",
    ".eml": "message/rfc822",
    ".msg": "message/rfc822",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".html": "text/html",
    ".htm": "text/html",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".tiff": "image/tiff",
    ".bmp": "image/bmp",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".mbox": "application/mbox",
    ".zip": "application/zip",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
    ".ics": "text/calendar",
    ".vcf": "text/vcard",
}


def _ensure_dirs():
    """Create inbox/processed/failed dirs if they don't exist."""
    for d in [INBOX_DIR, PROCESSED_DIR, FAILED_DIR]:
        Path(d).mkdir(parents=True, exist_ok=True)


def _ingest_file(filepath: str, tenant_id: str):
    """Ingest a single file through the signal pipeline."""
    from storage.ingest.signal_ingestion import ingest_signal

    filename = os.path.basename(filepath)
    ext = os.path.splitext(filename)[1].lower()
    content_type = EXTENSION_MAP.get(ext, "application/octet-stream")

    with open(filepath, "rb") as f:
        raw_bytes = f.read()

    if len(raw_bytes) == 0:
        logger.warning(f"[WATCH] Skipping empty file: {filename}")
        return None

    # Archives get special handling
    if ext in (".zip", ".tar", ".gz", ".tgz", ".mbox"):
        return _ingest_archive(filepath, raw_bytes, tenant_id)

    result = ingest_signal(
        raw_bytes=raw_bytes,
        device_id="file-drop",
        tenant_id=tenant_id,
        content_type=content_type,
        source_type="file_drop",
    )

    logger.info(
        f"[WATCH] Ingested {filename}: path={result.path}, "
        f"type={result.signal_type}, confidence={result.confidence:.2f}"
    )
    return result


def _ingest_archive(filepath: str, raw_bytes: bytes, tenant_id: str):
    """Handle archive files — split into chunks and ingest each one."""
    try:
        from storage.ingest.archive_splitter import split_and_ingest
        result = split_and_ingest(
            raw_bytes=raw_bytes,
            filename=os.path.basename(filepath),
            tenant_id=tenant_id,
            device_id="file-drop",
            source_type="archive_import",
        )
        logger.info(
            f"[WATCH] Archive split: {os.path.basename(filepath)}, "
            f"chunks={result.get('total', 0)}, ingested={result.get('accepted', 0)}"
        )
        return result
    except ImportError:
        # Fallback: ingest as single blob if splitter unavailable
        from storage.ingest.signal_ingestion import ingest_signal
        result = ingest_signal(
            raw_bytes=raw_bytes,
            device_id="file-drop",
            tenant_id=tenant_id,
            content_type="application/zip",
            source_type="archive_import",
        )
        logger.info(f"[WATCH] Archive ingested as blob: {os.path.basename(filepath)}")
        return result


def _process_inbox(tenant_id: str):
    """Scan inbox directory and ingest any new files."""
    inbox = Path(INBOX_DIR)
    if not inbox.exists():
        return

    for filepath in sorted(inbox.iterdir()):
        if filepath.is_dir():
            continue
        if filepath.name.startswith("."):
            continue  # skip hidden files

        str_path = str(filepath)
        try:
            result = _ingest_file(str_path, tenant_id)
            # Move to processed
            dest = Path(PROCESSED_DIR) / filepath.name
            if dest.exists():
                dest = Path(PROCESSED_DIR) / f"{filepath.stem}_{int(time.time())}{filepath.suffix}"
            shutil.move(str_path, str(dest))
            logger.info(f"[WATCH] Moved to processed: {filepath.name}")

        except Exception as e:
            logger.error(f"[WATCH] Failed to ingest {filepath.name}: {e}")
            # Move to failed
            dest = Path(FAILED_DIR) / filepath.name
            if dest.exists():
                dest = Path(FAILED_DIR) / f"{filepath.stem}_{int(time.time())}{filepath.suffix}"
            try:
                shutil.move(str_path, str(dest))
            except Exception:
                pass


def _watcher_loop(tenant_id: str):
    """Main loop — polls inbox every N seconds."""
    logger.info(f"[WATCH] Watch folder daemon started. Inbox: {INBOX_DIR}")
    _ensure_dirs()

    while not _watcher_stop.is_set():
        try:
            _process_inbox(tenant_id)
        except Exception as e:
            logger.error(f"[WATCH] Error in watch loop: {e}")

        _watcher_stop.wait(POLL_INTERVAL)

    logger.info("[WATCH] Watch folder daemon stopped.")


def start_watch_folder(tenant_id: str = "default"):
    """Start the watch folder daemon in a background thread."""
    global _watcher_thread
    if _watcher_thread and _watcher_thread.is_alive():
        return

    _watcher_stop.clear()
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        args=(tenant_id,),
        daemon=True,
        name="enki-watch-folder",
    )
    _watcher_thread.start()


def stop_watch_folder():
    """Stop the watch folder daemon."""
    _watcher_stop.set()
    if _watcher_thread:
        _watcher_thread.join(timeout=10)
