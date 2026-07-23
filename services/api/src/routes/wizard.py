"""
First-Run Wizard — the complete guided experience from boot to operational.

This is the entry point for new users. It detects the system state and walks
through everything needed to go from a fresh install to a working platform.

PUBLIC endpoints (pre-auth, needed for first-time setup):
  GET  /wizard/status          — system state: what's done, what's next
  GET  /wizard/invite-code     — show the bootstrap invite code (first boot only)

AUTH-REQUIRED endpoints (after Logto login):
  GET  /wizard/auth-check      — confirm auth is working, show user info
  GET  /wizard/steps           — full step list with completion status
  POST /wizard/complete-step   — mark a wizard step as done

The wizard step order:
  0. welcome       — explain Enki, show install mode (hardware vs cloud)
  1. auth          — create Logto account (if not logged in) or confirm login
  2. infrastructure — database, AI model, GDU (delegates to /onboarding)
  3. interests     — select topic bundles (delegates to /intelligence-setup)
  4. sources       — curate RSS feeds (delegates to /intelligence-setup)
  5. feeds         — toggle free data feeds (delegates to /intelligence-setup)
  6. api_keys      — optional API key registration
  7. location      — set home coordinates
  8. review        — summary + activate
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger("enki.wizard")
router = APIRouter(prefix="/wizard", tags=["wizard"])


# ─── Public endpoints (pre-auth) ─────────────────────────────────────────────

@router.get("/status")
async def wizard_status():
    """System state check — what's configured, what's missing, what's next.

    This is the FIRST call the UI makes. It determines whether to show:
    - The welcome/signup page (no users yet)
    - The wizard (user exists but setup incomplete)
    - The dashboard (setup complete)

    No auth required — must work before any user exists.
    """
    from storage.postgres.db import get_connection

    status = {
        "database": False,
        "has_users": False,
        "has_invite_code": False,
        "auth_configured": False,
        "ai_available": False,
        "setup_completed": False,
        "install_mode": os.environ.get("ENKI_PROFILE", "dev"),
        "profile": os.environ.get("ENKI_PROFILE", "(unset)"),
    }

    # Check database
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Database works
                status["database"] = True

                # Any users exist?
                cur.execute("SELECT COUNT(*) FROM identities")
                status["has_users"] = cur.fetchone()[0] > 0

                # Any invite codes exist?
                cur.execute("SELECT COUNT(*) FROM invite_codes WHERE status = 'pending'")
                status["has_invite_code"] = cur.fetchone()[0] > 0

                # Setup completed?
                cur.execute("SELECT setup_completed FROM setup_config LIMIT 1")
                row = cur.fetchone()
                status["setup_completed"] = bool(row and row[0])
    except Exception as e:
        logger.warning("Wizard status check failed: %s", e)

    # Check Logto
    logto_endpoint = os.environ.get("LOGTO_ENDPOINT", "")
    logto_app_id = os.environ.get("LOGTO_APP_ID", "")
    status["auth_configured"] = bool(logto_endpoint and logto_app_id)
    status["logto_endpoint"] = logto_endpoint

    # Check AI
    try:
        import urllib.request
        ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
        with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read())
            status["ai_available"] = bool(data.get("models"))
            status["ai_models"] = [m.get("name") for m in data.get("models", [])[:5]]
    except Exception:
        pass

    # Determine what the UI should show
    if not status["database"]:
        status["next_action"] = "database_setup"
        status["message"] = "Database is not reachable. Run docker compose up to start services."
    elif not status["auth_configured"]:
        status["next_action"] = "auth_config"
        status["message"] = "Logto is not configured. Set LOGTO_APP_ID and LOGTO_APP_SECRET."
    elif not status["has_users"]:
        status["next_action"] = "first_signup"
        status["message"] = "No users yet. Use the invite code to create the first admin account."
    elif not status["setup_completed"]:
        status["next_action"] = "wizard"
        status["message"] = "Setup is not complete. Continue the wizard to configure your platform."
    else:
        status["next_action"] = "dashboard"
        status["message"] = "System is operational."

    return status


@router.get("/invite-code")
async def get_invite_code():
    """Return the first available invite code for display on the welcome page.

    Only returns a code if NO users exist yet (first-boot scenario).
    After the first user signs up, this endpoint returns nothing —
    subsequent codes must be generated by an admin.
    """
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Only show code if no users exist (first boot)
                cur.execute("SELECT COUNT(*) FROM identities")
                if cur.fetchone()[0] > 0:
                    return {"code": None, "reason": "Users already exist. Ask your admin for an invite code."}

                # Get the first pending invite code
                cur.execute("""
                    SELECT code, tier, max_users, created_at
                    FROM invite_codes
                    WHERE status = 'pending'
                    ORDER BY created_at ASC LIMIT 1
                """)
                row = cur.fetchone()
                if row:
                    return {
                        "code": row[0],
                        "tier": row[1],
                        "max_users": row[2],
                        "instruction": (
                            "This is your invite code. Use it to create your admin account. "
                            "Copy it now — after signup, it will no longer be shown here."
                        ),
                    }
                return {"code": None, "reason": "No invite codes generated. Run enki-bootstrap.py first."}
    except Exception as e:
        return {"code": None, "reason": str(e)}


@router.get("/welcome")
async def welcome_info():
    """Welcome page content — explains what Enki is, shows install mode,
    and provides the steps that will follow.

    No auth required — this is the landing page for first-time visitors.
    """
    profile = os.environ.get("ENKI_PROFILE", "dev")
    is_hardware = profile in ("hardware", "hardened")

    steps = [
        {
            "step": 0,
            "id": "welcome",
            "title": "Welcome to Enki",
            "description": "Your all-source intelligence platform",
        },
        {
            "step": 1,
            "id": "auth",
            "title": "Create Your Account",
            "description": (
                "Sign in with the hardware key to authenticate"
                if is_hardware else
                "Create a secure account with email and password"
            ),
        },
        {
            "step": 2,
            "id": "infrastructure",
            "title": "System Check",
            "description": "Verify database, AI model, and governance are operational",
        },
        {
            "step": 3,
            "id": "interests",
            "title": "What Do You Care About?",
            "description": "Select topics — we'll configure your Intelligence feed automatically",
        },
        {
            "step": 4,
            "id": "sources",
            "title": "Curate Your Sources",
            "description": "Browse 85+ curated RSS feeds across 18 categories",
        },
        {
            "step": 5,
            "id": "feeds",
            "title": "Free Data Feeds",
            "description": "Toggle on weather, aircraft tracking, earthquakes, and more — no API keys needed",
        },
        {
            "step": 6,
            "id": "api_keys",
            "title": "Optional API Keys",
            "description": "Unlock additional data sources with free registrations",
        },
        {
            "step": 7,
            "id": "location",
            "title": "Set Your Location",
            "description": "Configure local weather, nearby airports, and regional data",
        },
        {
            "step": 8,
            "id": "review",
            "title": "Review & Launch",
            "description": "Confirm your setup and start ingesting intelligence",
        },
    ]

    auth_info = {}
    if is_hardware:
        auth_info = {
            "mode": "hardware",
            "title": "Hardware Key Authentication",
            "description": (
                "This Enki Station uses a physical hardware key (HWKEY watch) "
                "for admin operations. The watch must be plugged in via USB-C "
                "to approve device enrollment, grant issuance, and key operations. "
                "For daily use, you'll log in with your Logto account."
            ),
            "requirements": [
                "HWKEY hardware key plugged in for admin operations",
                "Logto account for daily web access",
                "Physical button press on watch to approve admin actions",
            ],
        }
    else:
        auth_info = {
            "mode": "cloud",
            "title": "Cloud Authentication",
            "description": (
                "This Enki instance uses Logto for authentication. You'll create "
                "an account with your email and password. The first account created "
                "automatically becomes the admin. Admin operations are signed with "
                "a software key stored on the server."
            ),
            "requirements": [
                "Email address for account creation",
                "Invite code (shown on this page for first-time setup)",
                "Password (minimum 8 characters, set during Logto signup)",
            ],
        }

    return {
        "install_mode": "hardware" if is_hardware else "cloud",
        "auth": auth_info,
        "steps": steps,
        "total_steps": len(steps),
    }


# ─── Auth-required endpoints ─────────────────────────────────────────────────

@router.get("/auth-check")
async def auth_check(request: Request):
    """Confirm that authentication is working. Called after Logto callback.
    Returns the user's identity info and role.

    Uses optional auth — returns auth state whether logged in or not.
    """
    from services.api.src.unified_auth import optional_unified_auth

    auth = await optional_unified_auth(request)
    if not auth:
        return {
            "authenticated": False,
            "message": "Not logged in. Create an account or sign in to continue.",
        }

    # Get identity details
    identity = None
    try:
        from storage.postgres.db import get_connection
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT identity_id, display_name, role, created_at
                    FROM identities
                    WHERE logto_sub = %s AND tenant_id = %s
                    LIMIT 1
                """, (auth.subject_id, auth.tenant_id))
                row = cur.fetchone()
                if row:
                    identity = {
                        "identity_id": row[0],
                        "display_name": row[1],
                        "role": row[2],
                        "created_at": row[3].isoformat() if row[3] else None,
                    }
    except Exception:
        pass

    return {
        "authenticated": True,
        "subject_id": auth.subject_id,
        "tenant_id": auth.tenant_id,
        "role": auth.role,
        "identity": identity,
        "message": f"Welcome{', ' + identity['display_name'] if identity else ''}! You're logged in as {auth.role}.",
    }


from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo


@router.get("/steps")
async def get_steps(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Full step list with completion status for the current user."""
    from storage.postgres.db import get_connection

    steps = {
        "auth": True,  # If they're calling this, auth works
        "infrastructure": False,
        "interests": False,
        "sources": False,
        "feeds": False,
        "api_keys": False,
        "location": False,
        "review": False,
    }

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Infrastructure: check GDU + AI
                cur.execute("SELECT 1 FROM pg_tables WHERE tablename = 'signals' LIMIT 1")
                gdu_ok = cur.fetchone() is not None

                # Check AI
                ai_ok = False
                try:
                    import urllib.request
                    ollama_url = os.environ.get("OLLAMA_BASE_URL", "http://ollama:11434")
                    with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=3) as resp:
                        data = json.loads(resp.read())
                        ai_ok = bool(data.get("models"))
                except Exception:
                    pass

                steps["infrastructure"] = gdu_ok and ai_ok

                # Watch topics exist?
                cur.execute("""
                    SELECT COUNT(*) FROM watch_topics
                    WHERE tenant_id = %s AND status = 'active'
                """, (auth.tenant_id,))
                steps["interests"] = cur.fetchone()[0] > 0

                # RSS sources enabled?
                cur.execute("""
                    SELECT COUNT(*) FROM connectors
                    WHERE tenant_id = %s AND status = 'active' AND source_type = 'rss'
                """, (auth.tenant_id,))
                steps["sources"] = cur.fetchone()[0] > 0

                # Recipe feeds enabled?
                cur.execute("""
                    SELECT COUNT(*) FROM connectors
                    WHERE tenant_id = %s AND status = 'active' AND device_id LIKE 'recipe_%%'
                """, (auth.tenant_id,))
                steps["feeds"] = cur.fetchone()[0] > 0

                # API keys stored?
                cur.execute("""
                    SELECT COUNT(*) FROM secrets
                    WHERE tenant_id = %s AND category = 'api_key'
                """, (auth.tenant_id,))
                steps["api_keys"] = cur.fetchone()[0] > 0

                # Location set?
                cur.execute("""
                    SELECT COUNT(*) FROM user_preferences
                    WHERE tenant_id = %s AND preference_key = 'home_location'
                """, (auth.tenant_id,))
                steps["location"] = cur.fetchone()[0] > 0

    except Exception as e:
        logger.warning("Step status check failed: %s", e)

    # Find the first incomplete step
    step_order = ["auth", "infrastructure", "interests", "sources", "feeds",
                  "api_keys", "location", "review"]
    current_step = "review"
    for s in step_order:
        if not steps.get(s):
            current_step = s
            break

    completed_count = sum(1 for v in steps.values() if v)

    return {
        "steps": steps,
        "current_step": current_step,
        "completed": completed_count,
        "total": len(steps),
        "progress_pct": round(completed_count / len(steps) * 100),
    }


# ─── Hardware Key Provisioning ───────────────────────────────────────────────

@router.get("/hardware-key/detect")
async def detect_hardware_key_status(auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Detect if a HWKEY hardware key is connected via USB.
    Returns the watch state: unflashed, ceremony needed, or operational.

    Used by the setup wizard to show the hardware key linking step.
    """
    try:
        # Try importing from enki-hardware-key package
        hw_key_path = os.environ.get(
            "ENKI_HARDWARE_KEY_PATH",
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "enki-hardware-key"),
        )
        sys.path.insert(0, hw_key_path)
        from server.flash import detect_unflashed_watch

        status = detect_unflashed_watch()
        if not status:
            return {
                "detected": False,
                "message": "No HWKEY detected. Plug in the watch via USB-C.",
                "profile": os.environ.get("ENKI_PROFILE", "dev"),
            }

        return {
            "detected": True,
            **status,
            "profile": os.environ.get("ENKI_PROFILE", "dev"),
        }
    except ImportError:
        return {
            "detected": False,
            "error": "enki-hardware-key package not found",
            "profile": os.environ.get("ENKI_PROFILE", "dev"),
        }
    except Exception as e:
        return {"detected": False, "error": str(e)}


@router.post("/hardware-key/provision")
async def provision_hardware_key(
    request: Request,
    auth: UnifiedAuthInfo = Depends(unified_auth),
):
    """Flash firmware + run ceremony + issue grant — single button.

    Body (all optional):
    {
        "device_id": "enki-hw-primary",   // default
        "port": "/dev/ttyUSB0",           // auto-detect if omitted
        "skip_flash": false,              // skip if firmware already present
        "lockscreen_style": 0             // 0=metatrons_shield, 1=simple_lock
    }

    This is the "Link Hardware Key" button in the setup wizard.
    """
    body = await request.json() if request.headers.get("content-length", "0") != "0" else {}
    device_id = body.get("device_id", "enki-hw-primary")
    port = body.get("port")
    skip_flash = body.get("skip_flash", False)
    lockscreen_style = body.get("lockscreen_style", 0)

    try:
        hw_key_path = os.environ.get(
            "ENKI_HARDWARE_KEY_PATH",
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..", "enki-hardware-key"),
        )
        sys.path.insert(0, hw_key_path)
        from server.flash import full_provision

        # Set the repo path so the flash script can issue grants
        os.environ.setdefault("ENKI_COMMAND_CENTER_PATH",
                              os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

        result = full_provision(
            port=port,
            device_id=device_id,
            skip_flash=skip_flash,
        )

        # Set lockscreen style if provisioning succeeded
        if result.get("success") and result.get("port"):
            try:
                from server.serial_transport import SerialTransport
                import time
                time.sleep(2)  # Wait for watch to stabilize after ceremony
                transport = SerialTransport(result["port"])
                transport.send_command({"cmd": "set_lockscreen", "style": lockscreen_style}, timeout=5.0)
                transport.close()
                result["lockscreen_style"] = lockscreen_style
            except Exception as e:
                logger.warning("Could not set lockscreen style: %s", e)

        return result

    except ImportError as e:
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": f"enki-hardware-key package not found: {e}",
        })
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "success": False,
            "error": str(e),
        })
