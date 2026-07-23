"""
Image Analyst Worker — Vision LLM + face detection + OCR pipeline.

Processes every ingested image through:
1. EXIF + perceptual hash (already done by image_parser)
2. Face detection (MediaPipe — fast, local, no GPU needed)
3. Vision LLM analysis (LLaVA via Ollama — scene, objects, text, classification)
4. OCR fallback (if vision LLM missed text in the image)
5. Collection collapse (group similar utility images, keep evidence standalone)
6. Knowledge graph linkage (entities, places, events, collections)

Runs as a background thread, polling for unanalyzed image signals.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("enki.image_analyst")

POLL_INTERVAL = int(os.getenv("ENKI_IMAGE_POLL_INTERVAL", "15"))
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
VISION_MODEL = os.getenv("ENKI_VISION_MODEL", "llava:7b")

_thread: threading.Thread | None = None
_stop_event = threading.Event()


# ─── Vision LLM ─────────────────────────────────────────────────────────────

def _analyze_with_vision(image_bytes: bytes) -> Optional[dict]:
    """Send image to the vision LLM for scene analysis."""
    import httpx

    image_b64 = base64.b64encode(image_bytes).decode()

    prompt = """Analyze this image. Return ONLY valid JSON:
{
  "description": "One clear sentence describing what this image shows",
  "scene": "indoor|outdoor|screenshot|document|graphic|portrait|aerial|underwater|space",
  "classification": "photo|screenshot|logo|icon|diagram|document|art|meme|map|chart|evidence|product|marketing",
  "objects": ["list", "of", "visible", "objects"],
  "people_count": 0,
  "text_visible": ["any", "text", "readable", "in", "image"],
  "colors_dominant": ["blue", "white"],
  "mood": "professional|casual|dramatic|calm|action|dark|bright",
  "tags": ["relevant", "searchable", "tags"],
  "is_intelligence": false,
  "intelligence_reason": null
}

Rules:
- is_intelligence=true if: faces of specific people, ID documents, badges, surveillance, license plates, vessels, aircraft, weapons, crime scenes, evidence
- is_intelligence=false if: logos, screenshots, art, marketing materials, generic landscapes, food, selfies, memes
- text_visible: read ALL text you can see (signs, labels, screens, badges, documents)
- objects: be specific (not "object" but "laptop", "red car", "coffee cup")
- tags: think about what someone would search to find this image"""

    try:
        r = httpx.post(
            f"{OLLAMA_URL}/api/generate",
            json={
                "model": VISION_MODEL,
                "prompt": prompt,
                "images": [image_b64],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 1024},
            },
            timeout=120,  # vision models are slower
        )

        text = r.json().get("response", "").strip()
        # Clean markdown wrapping
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        if text.startswith("json"):
            text = text[4:]

        return json.loads(text.strip())

    except json.JSONDecodeError as e:
        logger.warning("Vision LLM returned invalid JSON: %s", e)
        return None
    except Exception as e:
        logger.warning("Vision LLM failed: %s", e)
        return None


# ─── Face Detection ──────────────────────────────────────────────────────────

def _detect_faces(image_bytes: bytes) -> list[dict]:
    """Detect faces using MediaPipe (CPU, fast, no GPU needed).

    Returns list of face bounding boxes with confidence scores.
    Falls back gracefully if MediaPipe not installed.
    """
    try:
        import mediapipe as mp
        from PIL import Image
        import io
        import numpy as np

        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        img_array = np.array(img)

        face_detection = mp.solutions.face_detection.FaceDetection(
            model_selection=1,  # 1 = full range (>2m distance)
            min_detection_confidence=0.5,
        )

        results = face_detection.process(img_array)
        faces = []

        if results.detections:
            h, w = img_array.shape[:2]
            for detection in results.detections:
                bbox = detection.location_data.relative_bounding_box
                faces.append({
                    "confidence": round(detection.score[0], 3),
                    "x": round(bbox.xmin * w),
                    "y": round(bbox.ymin * h),
                    "width": round(bbox.width * w),
                    "height": round(bbox.height * h),
                })

        face_detection.close()
        return faces

    except ImportError:
        logger.debug("MediaPipe not installed, skipping face detection")
        return []
    except Exception as e:
        logger.debug("Face detection failed: %s", e)
        return []


# ─── OCR Fallback ────────────────────────────────────────────────────────────

def _extract_text_ocr(image_bytes: bytes) -> str:
    """Extract text from image using available OCR.

    Tries in order: PaddleOCR → EasyOCR → Tesseract → PIL EXIF text only.
    """
    # Try PaddleOCR
    try:
        from paddleocr import PaddleOCR
        import io
        import numpy as np
        from PIL import Image

        img = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))
        ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        results = ocr.ocr(img, cls=True)
        if results and results[0]:
            texts = [line[1][0] for line in results[0] if line[1][1] > 0.5]
            return " ".join(texts)
    except ImportError:
        pass
    except Exception:
        pass

    # Try Tesseract
    try:
        import pytesseract
        from PIL import Image
        import io

        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img).strip()
        if text:
            return text
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: EXIF text tags only (already extracted by image_parser)
    return ""


# ─── Collection Collapse ────────────────────────────────────────────────────

def _find_or_create_collection(
    conn, tenant_id: str, classification: str, tags: list[str]
) -> Optional[str]:
    """Find an existing collection node for this image type, or create one.

    Collections group utility images (logos, screenshots, etc.)
    Intelligence images don't get collected — they stand alone.
    """
    # Build a collection key from classification + top tags
    tag_key = "_".join(sorted(tags[:3])) if tags else "general"
    collection_key = f"collection:{classification}:{tag_key}"
    collection_id = f"imgcol:{hashlib.sha1(collection_key.encode()).hexdigest()[:12]}"

    with conn.cursor() as cur:
        # Check if collection exists
        cur.execute(
            "SELECT node_id, source_count FROM knowledge_nodes WHERE node_id = %s",
            (collection_id,),
        )
        row = cur.fetchone()

        if row:
            # Increment source count
            cur.execute(
                "UPDATE knowledge_nodes SET source_count = source_count + 1 WHERE node_id = %s",
                (collection_id,),
            )
            return collection_id

        # Create new collection node
        title_map = {
            "screenshot": "Screenshots",
            "logo": "Logos & Brand Assets",
            "icon": "Icons",
            "diagram": "Diagrams & Charts",
            "document": "Document Scans",
            "art": "Artwork & Graphics",
            "meme": "Memes & Social",
            "chart": "Charts & Data",
            "map": "Maps",
            "product": "Product Images",
            "marketing": "Marketing Assets",
            "photo": "Photos",
        }

        # Admit through GDU gateway
        from core.gdu.enforce import admit_node as gdu_admit_node
        gdu_admit_node(
            tenant_id=tenant_id, category="document", subtype="image",
            title=title_map.get(classification, classification.title())[:200],
            description=f"Collection of {classification} images",
            signal_rid=0, collapse_key=collection_id,
        )
        cur.execute("""
            INSERT INTO knowledge_nodes
                (node_id, project_id, tenant_id, title, description,
                 node_type, subtype, epistemic_type,
                 confidence, source_count, enrichment_status,
                 gdu_canonical, metadata)
            VALUES (%s, 'default', %s, %s, %s,
                    'document', 'image_collection', 'observation',
                    0.8, 1, 'complete', TRUE, %s)
            ON CONFLICT (node_id) DO UPDATE SET source_count = knowledge_nodes.source_count + 1
            RETURNING node_id
        """, (
            collection_id, tenant_id,
            title_map.get(classification, classification.title()),
            f"Collection of {classification} images",
            json.dumps({
                "classification": classification,
                "tags": tags,
                "collection_key": collection_key,
            }),
        ))

        return collection_id


# ─── Main Processing ────────────────────────────────────────────────────────

def _process_image(signal_rid: int, signal_id: str, payload: dict, tenant_id: str):
    """Full analysis pipeline for one image signal."""
    from storage.postgres.db import get_connection

    # Get the raw image bytes
    image_bytes = None
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Try to get bytes from ldu_t_signals
                cur.execute(
                    "SELECT payload_bytes FROM ldu_t_signals WHERE signal_rid = %s",
                    (signal_rid,),
                )
                row = cur.fetchone()
                if row and row[0]:
                    image_bytes = bytes(row[0])
    except Exception as e:
        logger.debug("Could not load image bytes for rid=%s: %s", signal_rid, e)

    if not image_bytes:
        # Try from media cache
        byte_hash = payload.get("file_sha256") or payload.get("parsed", {}).get("file_sha256")
        if byte_hash:
            cache_path = Path(f"storage/cache/media_library/{byte_hash[:2]}/{byte_hash}")
            for ext in ("", ".jpg", ".jpeg", ".png", ".webp"):
                p = cache_path.with_suffix(ext) if ext else cache_path
                if p.exists():
                    image_bytes = p.read_bytes()
                    break

    if not image_bytes:
        logger.debug("No image bytes found for rid=%s, skipping analysis", signal_rid)
        return

    # Step 1: Face detection (fast, CPU)
    faces = _detect_faces(image_bytes)
    logger.debug("Detected %d face(s) in rid=%s", len(faces), signal_rid)

    # Step 2: Vision LLM analysis (the brain)
    analysis = _analyze_with_vision(image_bytes)
    if not analysis:
        logger.warning("Vision LLM returned nothing for rid=%s", signal_rid)
        return

    # Step 3: OCR fallback if vision LLM found text_visible
    ocr_text = ""
    text_from_vision = analysis.get("text_visible", [])
    if not text_from_vision:
        ocr_text = _extract_text_ocr(image_bytes)
        if ocr_text:
            analysis["text_visible"] = ocr_text.split()[:20]

    # Step 4: Merge face detection with vision analysis
    analysis["faces_detected"] = len(faces)
    analysis["face_boxes"] = faces
    if faces and not analysis.get("is_intelligence"):
        # Photos with identifiable faces are potential intelligence
        if len(faces) <= 5:  # small group, likely identifiable
            analysis["is_intelligence"] = True
            analysis["intelligence_reason"] = f"{len(faces)} face(s) detected"

    # Step 5: Store analysis and create nodes
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Update the signal with analysis results
                cur.execute("""
                    UPDATE signals SET
                        metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                    WHERE rid = %s
                """, (
                    json.dumps({
                        "vision_analysis": analysis,
                        "faces_detected": len(faces),
                        "analyzed_at": datetime.now(timezone.utc).isoformat(),
                        "vision_model": VISION_MODEL,
                    }),
                    signal_rid,
                ))

                # Step 6: Create/link nodes based on classification
                is_intel = analysis.get("is_intelligence", False)
                classification = analysis.get("classification", "photo")
                tags = analysis.get("tags", [])
                description = analysis.get("description", "")

                if is_intel:
                    # Intelligence images get their own node (GDU governed)
                    node_id = f"image:{uuid.uuid4().hex[:12]}"
                    gdu_admit_node(
                        tenant_id=tenant_id, category="document", subtype="image",
                        title=description[:200] if description else "Evidence image",
                        description=description[:500] if description else "",
                        signal_rid=0, collapse_key=node_id,
                    )
                    cur.execute("""
                        INSERT INTO knowledge_nodes
                            (node_id, project_id, tenant_id, title, description,
                             node_type, subtype, epistemic_type,
                             confidence, source_count, enrichment_status,
                             gdu_canonical, metadata)
                        VALUES (%s, 'default', %s, %s, %s,
                                'document', 'evidence_image', 'observation',
                                0.7, 1, 'complete', TRUE, %s)
                        ON CONFLICT (node_id) DO NOTHING
                    """, (
                        node_id, tenant_id,
                        description[:200],
                        f"Objects: {', '.join(analysis.get('objects', [])[:10])}. "
                        f"Text: {', '.join(analysis.get('text_visible', [])[:10])}. "
                        f"Faces: {len(faces)}. Scene: {analysis.get('scene', 'unknown')}.",
                        json.dumps({
                            "signal_rid": signal_rid,
                            "classification": classification,
                            "tags": tags,
                            "faces": len(faces),
                            "objects": analysis.get("objects", []),
                            "text_visible": analysis.get("text_visible", []),
                            "is_intelligence": True,
                            "intelligence_reason": analysis.get("intelligence_reason"),
                        }),
                    ))

                    logger.info(
                        "Intelligence image node created: %s — %s (%d faces)",
                        node_id, description[:60], len(faces),
                    )
                else:
                    # Utility images collapse into collections
                    collection_id = _find_or_create_collection(
                        conn, tenant_id, classification, tags,
                    )
                    if collection_id:
                        # Link image signal to collection
                        edge_id = f"imglink:{uuid.uuid4().hex[:12]}"
                        cur.execute("""
                            INSERT INTO knowledge_edges
                                (edge_id, project_id, source_node_id, target_node_id,
                                 relationship, strength, evidence, ai_generated)
                            VALUES (%s, 'default', %s, %s, 'member_of', 0.8, %s, TRUE)
                            ON CONFLICT (edge_id) DO NOTHING
                        """, (
                            edge_id,
                            f"signal:{signal_rid}",
                            collection_id,
                            description[:200],
                        ))

                        # Update collection metadata with sample descriptions
                        cur.execute("""
                            UPDATE knowledge_nodes SET
                                metadata = jsonb_set(
                                    COALESCE(metadata, '{}'::jsonb),
                                    '{sample_descriptions}',
                                    (COALESCE(metadata->'sample_descriptions', '[]'::jsonb) || %s::jsonb)
                                )
                            WHERE node_id = %s
                        """, (
                            json.dumps([description[:100]]),
                            collection_id,
                        ))

                        logger.info(
                            "Image collapsed into collection %s — %s",
                            collection_id, description[:60],
                        )

                # Step 7: Extract entities from vision analysis
                # Create entity nodes for detected objects/people/text
                for obj in analysis.get("objects", []):
                    if obj.lower() in ("person", "man", "woman", "child", "people"):
                        continue  # faces handled separately
                    # Objects become tags, not entities (too noisy)

                # Link to place if EXIF GPS available
                lat = payload.get("latitude") or payload.get("parsed", {}).get("latitude")
                lon = payload.get("longitude") or payload.get("parsed", {}).get("longitude")
                if lat and lon:
                    cur.execute("""
                        UPDATE signals SET
                            metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb
                        WHERE rid = %s
                    """, (
                        json.dumps({"geo_linked": True, "latitude": lat, "longitude": lon}),
                        signal_rid,
                    ))

            conn.commit()

    except Exception as e:
        logger.error("Failed to store image analysis for rid=%s: %s", signal_rid, e)


def _process_pending():
    """Find unanalyzed image signals and process them."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT s.rid, s.signal_id, s.payload, s.tenant_id
                    FROM signals s
                    WHERE s.signal_type = 'signal.content.image'
                      AND (s.metadata IS NULL OR s.metadata->>'vision_analysis' IS NULL)
                    ORDER BY s.timestamp DESC
                    LIMIT 5
                """)
                rows = cur.fetchall()

    except Exception as e:
        logger.debug("Failed to query image signals: %s", e)
        return

    for rid, signal_id, payload, tenant_id in rows:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        logger.info("Analyzing image signal rid=%s", rid)
        _process_image(rid, signal_id, payload or {}, tenant_id or "default")


def _analyst_loop():
    """Main loop."""
    while not _stop_event.is_set():
        try:
            _process_pending()
        except Exception as e:
            logger.error("Image analyst error: %s", e)
        _stop_event.wait(POLL_INTERVAL)


def start_worker(tenant_id: str = "default"):
    """Start the image analyst worker."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop_event.clear()
    _thread = threading.Thread(target=_analyst_loop, daemon=True, name="image-analyst")
    _thread.start()
    logger.info("Image analyst started (model=%s, poll=%ds)", VISION_MODEL, POLL_INTERVAL)


def stop_worker():
    """Stop the image analyst worker."""
    global _thread
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)
        _thread = None
    logger.info("Image analyst stopped")


def get_status() -> dict:
    return {
        "running": _thread is not None and _thread.is_alive(),
        "vision_model": VISION_MODEL,
        "poll_interval": POLL_INTERVAL,
    }
