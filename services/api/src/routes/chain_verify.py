"""
Chain Verification Endpoints — on-demand and status for GDU hash chain integrity.
"""

from fastapi import APIRouter, Depends
from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo

router = APIRouter(prefix="/governance", tags=["governance"])


@router.post("/chain/verify")
async def verify_chain_now(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Run chain verification immediately and return result."""
    try:
        from core.workers.chain_verifier import verify_now
        result = verify_now(auth.tenant_id)
        return result
    except Exception as e:
        return {"valid": False, "error": str(e)}


@router.get("/chain/verification-status")
async def chain_verification_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Last chain verification result from the background worker."""
    try:
        from core.workers.chain_verifier import get_chain_verifier_status
        status = get_chain_verifier_status()
        return {
            "worker_running": status.get("running", False),
            "last_result": status.get("last_result"),
            "interval_s": status.get("interval_s", 1800),
        }
    except Exception as e:
        return {"worker_running": False, "error": str(e)}
