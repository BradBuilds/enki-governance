"""
Text-to-Speech API — generates audio from text using Google Cloud TTS or OpenAI.

GET  /tts/voices               — list available voices for current provider
POST /tts/generate             — generate audio for text, returns MP3 (cached on disk)
GET  /tts/audio/{cache_key}    — stream cached audio
GET  /tts/status               — current provider + config status

Audio is cached on disk by sha256(text + voice + provider) — replays are free.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Body
from fastapi.responses import FileResponse
from pydantic import BaseModel

from services.api.src.unified_auth import unified_auth, UnifiedAuthInfo, require_role

log = logging.getLogger("enki.tts")
router = APIRouter(prefix="/tts", tags=["tts"])


# ─── Configuration ──────────────────────────────────────────────────────

_default_cache = "/var/lib/enki/tts-cache"
CACHE_DIR = Path(os.environ.get("ENKI_TTS_CACHE_DIR", _default_cache))
try:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
except PermissionError:
    CACHE_DIR = Path("/tmp/enki-tts-cache")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Google Cloud TTS service account JSON path
GOOGLE_KEY_PATH = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/etc/enki/google-tts-key.json"
)

# Default voice settings — Studio is highest quality (broadcast-trained)
DEFAULT_GOOGLE_VOICE = "en-US-Studio-O"  # Studio female, news-anchor quality
DEFAULT_GOOGLE_HEADLINE_VOICE = "en-US-Studio-Q"  # Alternative for variety


# ─── Provider detection ─────────────────────────────────────────────────

def _google_available() -> bool:
    """Check if Google Cloud TTS is configured and credentials exist."""
    if not os.path.exists(GOOGLE_KEY_PATH):
        return False
    try:
        from google.cloud import texttospeech  # noqa
        return True
    except Exception:
        return False


def _openai_available() -> bool:
    """Check if OpenAI API key is configured."""
    return bool(os.environ.get("OPENAI_API_KEY"))


# ─── Status ─────────────────────────────────────────────────────────────

@router.get("/status")
async def tts_status():
    """Report which TTS providers are available."""
    return {
        "providers": {
            "web_speech": {"available": True, "note": "Browser-built-in (default)"},
            "google": {
                "available": _google_available(),
                "key_path": GOOGLE_KEY_PATH,
                "note": "Google Cloud TTS — requires service account JSON",
            },
            "openai": {
                "available": _openai_available(),
                "note": "OpenAI TTS — requires OPENAI_API_KEY env var",
            },
        },
        "cache_dir": str(CACHE_DIR),
        "default_provider": "google" if _google_available() else "web_speech",
    }


# ─── Voices ─────────────────────────────────────────────────────────────

@router.get("/voices")
async def list_voices(provider: str = "google"):
    """List voices available for the given provider."""
    if provider == "google":
        if not _google_available():
            return {"voices": [], "error": "Google TTS not configured"}
        try:
            from google.cloud import texttospeech
            client = texttospeech.TextToSpeechClient.from_service_account_json(GOOGLE_KEY_PATH)
            response = client.list_voices(language_code="en-US")
            voices = []
            for v in response.voices:
                voices.append({
                    "name": v.name,
                    "language": v.language_codes[0] if v.language_codes else "en-US",
                    "gender": v.ssml_gender.name if v.ssml_gender else "NEUTRAL",
                    "natural_sample_rate": v.natural_sample_rate_hertz,
                    "tier": (
                        "Studio" if "Studio" in v.name else
                        "Neural2" if "Neural2" in v.name else
                        "Wavenet" if "Wavenet" in v.name else
                        "Standard"
                    ),
                })
            # Sort: Studio > Neural2 > Wavenet > Standard
            tier_order = {"Studio": 0, "Neural2": 1, "Wavenet": 2, "Standard": 3}
            voices.sort(key=lambda v: (tier_order.get(v["tier"], 9), v["name"]))
            return {"voices": voices, "provider": "google"}
        except Exception as e:
            return {"voices": [], "error": str(e)}

    elif provider == "openai":
        # OpenAI has 6 fixed voices
        return {
            "voices": [
                {"name": "alloy", "description": "Neutral, balanced"},
                {"name": "echo", "description": "Calm, measured (male)"},
                {"name": "fable", "description": "British, warm"},
                {"name": "onyx", "description": "Deep, authoritative (male)"},
                {"name": "nova", "description": "Energetic, friendly (female)"},
                {"name": "shimmer", "description": "Soft, gentle (female)"},
            ],
            "provider": "openai",
        }

    return {"voices": [], "error": f"Unknown provider: {provider}"}


# ─── SSML builder ───────────────────────────────────────────────────────

def _strip_ssml_unsafe(text: str) -> str:
    """Remove characters that would break SSML."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_ssml(headline: str, body: str, voice: str = "") -> str:
    """Build SSML with headline emphasis + pause + natural body pacing.
    Used for Google Cloud TTS only.

    Studio voices don't support pitch attributes — skip pitch for those.
    """
    h = _strip_ssml_unsafe(headline.strip()) if headline else ""
    b = _strip_ssml_unsafe(body.strip()) if body else ""
    is_studio = "Studio" in voice

    parts = ['<speak>']
    if h:
        # Studio: plain text + break only (no prosody/emphasis supported)
        # Neural2/Wavenet: full SSML with emphasis + pitch
        if is_studio:
            parts.append(h)
        else:
            parts.append(f'<prosody rate="0.92" pitch="+1st"><emphasis level="moderate">{h}</emphasis></prosody>')
        parts.append('<break time="900ms"/>')
    if b:
        body_with_breaks = re.sub(r'\n\s*\n', '<break time="500ms"/>', b)
        if is_studio:
            parts.append(body_with_breaks)
        else:
            parts.append(f'<prosody rate="1.0">{body_with_breaks}</prosody>')
    parts.append('</speak>')
    return ''.join(parts)


# ─── Cache ──────────────────────────────────────────────────────────────

def _cache_key(text: str, provider: str, voice: str, rate: float = 1.0) -> str:
    """Compute deterministic cache key."""
    h = hashlib.sha256()
    h.update(provider.encode())
    h.update(voice.encode())
    h.update(f"{rate:.2f}".encode())
    h.update(text.encode())
    return h.hexdigest()


def _cache_path(key: str) -> Path:
    return CACHE_DIR / f"{key}.mp3"


# ─── Generate ───────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    text: Optional[str] = None
    headline: Optional[str] = None
    body: Optional[str] = None
    provider: str = "google"  # google | openai
    voice: Optional[str] = None
    rate: float = 1.0


@router.post("/generate")
async def generate_audio(body: GenerateRequest, auth: UnifiedAuthInfo = Depends(unified_auth)):
    """Generate audio from text. Returns {audio_url, cache_key, cached}.

    If headline+body provided (Google only), uses SSML to emphasize headline.
    Otherwise falls back to single text reading.
    """
    provider = body.provider.lower()
    voice = body.voice or (DEFAULT_GOOGLE_VOICE if provider == "google" else "alloy")

    # Build content
    if body.headline and body.body and provider == "google":
        # SSML mode for Google
        text_for_cache = f"HEAD::{body.headline}\n\nBODY::{body.body}"
    else:
        # Plain text
        text_for_cache = body.text or f"{body.headline or ''}\n\n{body.body or ''}".strip()

    if not text_for_cache.strip():
        raise HTTPException(status_code=400, detail="text required")

    # Length cap (Google has 5000 char SSML limit)
    if len(text_for_cache) > 4500:
        text_for_cache = text_for_cache[:4500]
        if body.body and len(body.body) > 4400:
            body.body = body.body[:4400]

    cache_key = _cache_key(text_for_cache, provider, voice, body.rate)
    cache_file = _cache_path(cache_key)

    # Cache hit — return immediately
    if cache_file.exists() and cache_file.stat().st_size > 0:
        return {
            "audio_url": f"/api/tts/audio/{cache_key}",
            "cache_key": cache_key,
            "cached": True,
            "size_bytes": cache_file.stat().st_size,
        }

    # Generate via provider
    try:
        if provider == "google":
            await _generate_google(body, cache_file, voice)
        elif provider == "openai":
            await _generate_openai(body, cache_file, voice)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")
    except HTTPException:
        raise
    except Exception as e:
        log.error("TTS generation failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "audio_url": f"/api/tts/audio/{cache_key}",
        "cache_key": cache_key,
        "cached": False,
        "size_bytes": cache_file.stat().st_size if cache_file.exists() else 0,
    }


async def _generate_google(req: GenerateRequest, out_path: Path, voice: str):
    """Generate audio via Google Cloud TTS."""
    if not _google_available():
        raise HTTPException(status_code=503, detail="Google TTS not configured (missing credentials)")

    from google.cloud import texttospeech

    client = texttospeech.TextToSpeechClient.from_service_account_json(GOOGLE_KEY_PATH)

    # Use SSML if headline+body provided, else plain text
    if req.headline and req.body:
        ssml = build_ssml(req.headline, req.body, voice)
        synthesis_input = texttospeech.SynthesisInput(ssml=ssml)
    elif req.headline:
        ssml = build_ssml(req.headline, "", voice)
        synthesis_input = texttospeech.SynthesisInput(ssml=ssml)
    else:
        synthesis_input = texttospeech.SynthesisInput(text=req.text or req.body or "")

    voice_params = texttospeech.VoiceSelectionParams(
        language_code="en-US",
        name=voice,
    )
    # Studio voices don't accept speaking_rate (rate baked into model)
    if "Studio" in voice:
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
        )
    else:
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=req.rate,
        )

    response = client.synthesize_speech(
        input=synthesis_input,
        voice=voice_params,
        audio_config=audio_config,
    )

    out_path.write_bytes(response.audio_content)
    log.info("Google TTS generated %d bytes for voice=%s", len(response.audio_content), voice)


async def _generate_openai(req: GenerateRequest, out_path: Path, voice: str):
    """Generate audio via OpenAI TTS."""
    if not _openai_available():
        raise HTTPException(status_code=503, detail="OpenAI TTS not configured (missing OPENAI_API_KEY)")

    try:
        from openai import OpenAI
    except ImportError:
        raise HTTPException(status_code=503, detail="openai package not installed")

    client = OpenAI()
    text = req.text or f"{req.headline or ''}. {req.body or ''}".strip()

    response = client.audio.speech.create(
        model="tts-1",  # tts-1-hd for higher quality
        voice=voice,
        input=text[:4000],  # OpenAI cap
        speed=max(0.25, min(4.0, req.rate)),
    )
    out_path.write_bytes(response.content)
    log.info("OpenAI TTS generated for voice=%s", voice)


# ─── Stream cached audio ────────────────────────────────────────────────

@router.get("/audio/{cache_key}")
async def get_audio(cache_key: str):
    """Stream cached MP3 audio."""
    # Validate key (sha256 = 64 hex chars)
    if not re.fullmatch(r"[a-f0-9]{64}", cache_key):
        raise HTTPException(status_code=400, detail="invalid cache key")
    path = _cache_path(cache_key)
    if not path.exists():
        raise HTTPException(status_code=404, detail="audio not found")
    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ─── Configure credentials from UI ──────────────────────────────────────

class ConfigureGoogleRequest(BaseModel):
    service_account_json: str  # JSON text pasted from Google Cloud console


@router.post("/configure/google")
async def configure_google(body: ConfigureGoogleRequest, auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    """Save a Google Cloud service account JSON key and activate TTS. Admin only."""
    try:
        data = json.loads(body.service_account_json)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON — paste the full contents of the service account key file")

    required = {"type", "project_id", "private_key", "client_email"}
    if not required.issubset(set(data.keys())):
        raise HTTPException(status_code=400,
            detail=f"JSON missing required fields: {required - set(data.keys())}. "
                   f"This must be a Google service account key file.")
    if data.get("type") != "service_account":
        raise HTTPException(status_code=400,
            detail=f"Expected 'type: service_account' but got '{data.get('type')}'. "
                   f"Make sure you downloaded a service account key, not an OAuth client ID.")

    # Ensure dir exists
    key_dir = Path(GOOGLE_KEY_PATH).parent
    try:
        key_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise HTTPException(status_code=500,
            detail=f"Cannot create directory {key_dir}. Set GOOGLE_APPLICATION_CREDENTIALS to a writable path, "
                   f"or create {key_dir} manually with proper ownership.")

    try:
        with open(GOOGLE_KEY_PATH, "w") as f:
            f.write(body.service_account_json)
        os.chmod(GOOGLE_KEY_PATH, 0o644)
    except PermissionError:
        raise HTTPException(status_code=500,
            detail=f"Cannot write to {GOOGLE_KEY_PATH}. Check permissions or set GOOGLE_APPLICATION_CREDENTIALS env var.")

    # Validate by making a tiny test call
    try:
        from google.cloud import texttospeech
        client = texttospeech.TextToSpeechClient.from_service_account_json(GOOGLE_KEY_PATH)
        # List voices as smoke test (doesn't consume quota)
        client.list_voices(language_code="en-US")
    except Exception as e:
        raise HTTPException(status_code=400,
            detail=f"Credentials saved but validation failed: {str(e)[:300]}. "
                   f"Make sure the Text-to-Speech API is enabled for project {data.get('project_id')}.")

    return {
        "status": "configured",
        "project_id": data.get("project_id"),
        "client_email": data.get("client_email"),
        "message": "Google Cloud TTS is now active. Reader will use it by default.",
    }


class ConfigureOpenAIRequest(BaseModel):
    api_key: str


@router.post("/configure/openai")
async def configure_openai(body: ConfigureOpenAIRequest, auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    """Save OpenAI API key for TTS. Admin only."""
    key = body.api_key.strip()
    if not key.startswith("sk-"):
        raise HTTPException(status_code=400, detail="OpenAI API keys start with 'sk-'")

    # Validate by making a tiny test
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        # Lightweight call: list models
        client.models.list()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Key validation failed: {str(e)[:200]}")

    # Save to env config file
    env_file = "/etc/enki/tts.env"
    try:
        Path(env_file).parent.mkdir(parents=True, exist_ok=True)
        with open(env_file, "w") as f:
            f.write(f"OPENAI_API_KEY={key}\n")
        os.chmod(env_file, 0o600)
    except PermissionError:
        raise HTTPException(status_code=500, detail=f"Cannot write to {env_file}")

    # Also set for current process
    os.environ["OPENAI_API_KEY"] = key

    return {
        "status": "configured",
        "message": "OpenAI TTS is now active. Restart may be required for full effect.",
    }


@router.delete("/configure/google")
async def remove_google_config(auth: UnifiedAuthInfo = Depends(require_role("admin"))):
    """Remove Google Cloud TTS credentials. Admin only."""
    try:
        if os.path.exists(GOOGLE_KEY_PATH):
            os.remove(GOOGLE_KEY_PATH)
        return {"status": "removed"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
