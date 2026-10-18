"""
Static file downloads for Enki Pulse agents.

GET /downloads/EnkiPulse.apk  — Android APK
GET /downloads/EnkiPulse.exe  — Windows EXE
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(prefix="/downloads", tags=["downloads"])

DOWNLOADS_DIR = Path("/data/downloads")


@router.get("/{filename}")
async def download_file(filename: str):
    """Serve agent downloads (APK, EXE)."""
    # Only allow specific files
    allowed = {"EnkiPulse.apk", "EnkiPulse.exe"}
    if filename not in allowed:
        return JSONResponse(status_code=404, content={"error": "File not found"})

    filepath = DOWNLOADS_DIR / filename
    if not filepath.exists():
        return JSONResponse(status_code=404, content={"error": f"{filename} not available"})

    media_type = {
        ".apk": "application/vnd.android.package-archive",
        ".exe": "application/x-msdownload",
    }.get(filepath.suffix, "application/octet-stream")

    return FileResponse(
        path=str(filepath),
        filename=filename,
        media_type=media_type,
    )
