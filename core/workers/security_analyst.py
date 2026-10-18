"""
Security Analyst Worker — LLM-powered threat analysis.

When a security event is detected (AppArmor denial, suspicious process,
governance tampering, unknown SSH), this worker:

1. Gathers full context: parent process tree, file hash, network connections,
   user account, recent commands, related signals
2. Sends everything to the local LLM for structured threat analysis
3. Creates an Assessment node with the LLM's analysis
4. Links the assessment to the triggering signal and any discovered entities
5. Fires an alert with the complete analysis attached

The LLM acts as a security analyst — it reads the raw evidence and writes
a structured report: what happened, where it came from, what it tried to do,
how dangerous it is, and what to do about it.

Runs as a background thread, polling for unanalyzed security signals.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("enki.security_analyst")

POLL_INTERVAL = int(os.getenv("ENKI_SECURITY_POLL_INTERVAL", "10"))
OLLAMA_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
LLM_MODEL = os.getenv("ENKI_LLM_MODEL", "gemma3:4b")

# Security signal types that trigger analysis
ANALYZABLE_TYPES = {
    "signal.security.suspicious_execution",
    "signal.security.policy_violation",
    "signal.security.kernel_modification",
    "signal.security.key_access",
    "signal.security.privilege_escalation",
    "signal.security.apparmor_denial",
    "signal.security.authentication",        # unknown SSH
    "signal.security.authentication_failure", # brute force
}

_thread: threading.Thread | None = None
_stop_event = threading.Event()


def _gather_context(signal_payload: dict) -> dict:
    """Gather additional system context around a security event.

    Pulls: process tree, file hash, network connections, user info,
    recent related signals.
    """
    context = {
        "event_time": signal_payload.get("timestamp", "unknown"),
        "signal_type": signal_payload.get("signal_type", "unknown"),
        "severity": signal_payload.get("severity", "unknown"),
        "raw_payload": signal_payload,
    }

    # Process info from payload
    exe = signal_payload.get("exe") or signal_payload.get("path", "")
    user = signal_payload.get("user", "unknown")
    pid = signal_payload.get("pid", "")
    source_ip = signal_payload.get("source_ip", "")

    context["executable"] = exe
    context["user"] = user
    context["pid"] = pid
    context["source_ip"] = source_ip

    # Try to get process tree (if PID available and process still exists)
    if pid:
        try:
            import subprocess
            result = subprocess.run(
                ["ps", "-o", "pid,ppid,user,args", "--forest", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
            context["process_tree"] = result.stdout.strip()
        except Exception:
            context["process_tree"] = "process no longer running"

    # Try to hash the executable
    if exe and os.path.isfile(exe):
        try:
            with open(exe, "rb") as f:
                context["file_sha256"] = hashlib.sha256(f.read()).hexdigest()
            context["file_size"] = os.path.getsize(exe)
            import subprocess
            result = subprocess.run(
                ["file", exe], capture_output=True, text=True, timeout=5,
            )
            context["file_type"] = result.stdout.strip()
        except Exception:
            pass

    # Recent network connections from the user
    if user and user != "unknown":
        try:
            import subprocess
            result = subprocess.run(
                ["ss", "-tunap"], capture_output=True, text=True, timeout=5,
            )
            # Filter to relevant connections
            lines = [l for l in result.stdout.split("\n") if user in l or exe in l]
            context["network_connections"] = lines[:10]
        except Exception:
            pass

    # Get recent commands from user's bash history (if readable)
    home = f"/home/{user}" if user not in ("root", "unknown") else "/root"
    history_file = os.path.join(home, ".bash_history")
    if os.path.isfile(history_file):
        try:
            with open(history_file, "r") as f:
                lines = f.readlines()
            context["recent_commands"] = [l.strip() for l in lines[-20:]]
        except Exception:
            pass

    # Check if this IP has been seen before
    if source_ip:
        try:
            from storage.postgres.db import get_connection
            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT COUNT(*), MIN(timestamp), MAX(timestamp)
                        FROM signals
                        WHERE payload->>'source_ip' = %s
                          AND signal_type LIKE 'signal.security.%%'
                    """, (source_ip,))
                    row = cur.fetchone()
                    if row:
                        context["ip_history"] = {
                            "total_events": row[0],
                            "first_seen": row[1].isoformat() if row[1] else None,
                            "last_seen": row[2].isoformat() if row[2] else None,
                        }
        except Exception:
            pass

    return context


def _analyze_with_llm(context: dict) -> Optional[dict]:
    """Send the security event context to the LLM for threat analysis."""
    try:
        import httpx
    except ImportError:
        import requests as httpx

    prompt = f"""You are a security analyst for an intelligence server called Enki.
A security event was detected. Analyze it and provide a structured threat assessment.

EVENT DETAILS:
- Type: {context.get('signal_type', 'unknown')}
- Severity: {context.get('severity', 'unknown')}
- Time: {context.get('event_time', 'unknown')}
- User: {context.get('user', 'unknown')}
- Executable: {context.get('executable', 'unknown')}
- PID: {context.get('pid', 'unknown')}
- Source IP: {context.get('source_ip', 'none')}

FILE INFO:
- SHA256: {context.get('file_sha256', 'unavailable')}
- Size: {context.get('file_size', 'unavailable')} bytes
- Type: {context.get('file_type', 'unavailable')}

PROCESS TREE:
{context.get('process_tree', 'unavailable')}

NETWORK CONNECTIONS:
{json.dumps(context.get('network_connections', []), indent=2)}

RECENT COMMANDS BY THIS USER:
{json.dumps(context.get('recent_commands', []), indent=2)}

IP HISTORY:
{json.dumps(context.get('ip_history', {}), indent=2)}

RAW EVENT DATA:
{json.dumps(context.get('raw_payload', {}), indent=2)[:2000]}

Respond with ONLY valid JSON in this format:
{{
  "threat_level": "critical|high|medium|low|informational",
  "classification": "malware|intrusion|privilege_escalation|data_exfiltration|policy_violation|reconnaissance|denial_of_service|legitimate",
  "what_happened": "One sentence describing the event",
  "origin": "Where this came from (IP, user, parent process, download source)",
  "intent": "What it was trying to accomplish",
  "impact": "What damage could result if not stopped",
  "indicators": ["list", "of", "IOCs", "found"],
  "recommended_action": "What the operator should do",
  "requires_watch_approval": true,
  "confidence": 0.85
}}"""

    try:
        if hasattr(httpx, 'Client'):
            # httpx
            r = httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": LLM_MODEL, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.1, "num_predict": 1024}},
                timeout=60,
            )
            response_text = r.json().get("response", "")
        else:
            # requests
            r = httpx.post(
                f"{OLLAMA_URL}/api/generate",
                json={"model": LLM_MODEL, "prompt": prompt, "stream": False,
                      "options": {"temperature": 0.1, "num_predict": 1024}},
                timeout=60,
            )
            response_text = r.json().get("response", "")

        # Parse JSON from LLM response
        # Strip markdown code blocks if present
        text = response_text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        if text.startswith("json"):
            text = text[4:]

        analysis = json.loads(text.strip())
        return analysis

    except json.JSONDecodeError as e:
        logger.warning("LLM returned invalid JSON: %s", e)
        return {
            "threat_level": "unknown",
            "classification": "unknown",
            "what_happened": "LLM analysis failed to parse",
            "raw_response": response_text[:500] if 'response_text' in dir() else "no response",
            "confidence": 0.0,
        }
    except Exception as e:
        logger.warning("LLM analysis failed: %s", e)
        return None


def _store_analysis(signal_rid: int, tenant_id: str, context: dict, analysis: dict):
    """Store the threat analysis as an Assessment node and create an alert."""
    from storage.postgres.db import get_connection

    threat_level = analysis.get("threat_level", "unknown")
    classification = analysis.get("classification", "unknown")
    what_happened = analysis.get("what_happened", "Security event detected")
    title = f"[{threat_level.upper()}] {classification}: {what_happened}"

    # Build rich description
    description = f"""## Threat Analysis

**Classification:** {classification}
**Threat Level:** {threat_level}
**Confidence:** {analysis.get('confidence', 'unknown')}

### What Happened
{what_happened}

### Origin
{analysis.get('origin', 'Unknown')}

### Intent
{analysis.get('intent', 'Unknown')}

### Potential Impact
{analysis.get('impact', 'Unknown')}

### Indicators of Compromise
{chr(10).join('- ' + ioc for ioc in analysis.get('indicators', []))}

### Recommended Action
{analysis.get('recommended_action', 'Investigate further')}

### Raw Context
- Executable: {context.get('executable', 'unknown')}
- User: {context.get('user', 'unknown')}
- File SHA256: {context.get('file_sha256', 'unavailable')}
- Source IP: {context.get('source_ip', 'none')}
"""

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # 1. Create assessment node (GDU governed)
                import uuid
                node_id = f"security-analysis-{uuid.uuid4().hex[:12]}"
                from core.gdu.enforce import admit_node as gdu_admit_node
                gdu_admit_node(
                    tenant_id=tenant_id, category="assessment", subtype="observation",
                    title=title[:200], description=(description or "")[:500],
                    signal_rid=0, collapse_key=node_id,
                )

                cur.execute("""
                    INSERT INTO knowledge_nodes
                        (node_id, project_id, tenant_id, title, description,
                         node_type, subtype, epistemic_type,
                         confidence, source_count, enrichment_status,
                         gdu_canonical, metadata)
                    VALUES (%s, 'default', %s, %s, %s,
                            'assessment', 'observation', 'observation',
                            %s, 1, 'complete',
                            TRUE, %s)
                    ON CONFLICT (node_id) DO NOTHING
                """, (
                    node_id, tenant_id, title[:500], description[:4000],
                    analysis.get("confidence", 0.5),
                    json.dumps({
                        "security_analysis": True,
                        "threat_level": threat_level,
                        "classification": classification,
                        "signal_rid": signal_rid,
                        "indicators": analysis.get("indicators", []),
                        "requires_watch_approval": analysis.get("requires_watch_approval", True),
                    }),
                ))

                # 2. Link assessment to triggering signal — deterministic
                #    edge_id so re-firing produces ON CONFLICT, not a duplicate
                #    edge with a different secedge-uuid suffix.
                _sec_src = node_id
                _sec_tgt = f"signal:{signal_rid}"
                _sec_rel = "derived_from"
                _sec_edge_id = hashlib.sha1(
                    f"{_sec_src}|{_sec_tgt}|{_sec_rel}".encode()
                ).hexdigest()[:16]
                cur.execute("""
                    INSERT INTO knowledge_edges
                        (edge_id, project_id, source_node_id, target_node_id,
                         relationship, strength, evidence, ai_generated)
                    VALUES (%s, 'default', %s, %s, 'derived_from', 0.95, %s, TRUE)
                    ON CONFLICT (edge_id) DO NOTHING
                """, (
                    _sec_edge_id, _sec_src, _sec_tgt,
                    f"Security analysis of signal {signal_rid}",
                ))

                # 3. Create alert
                severity_map = {
                    "critical": "critical", "high": "high",
                    "medium": "medium", "low": "low",
                    "informational": "low",
                }
                alert_severity = severity_map.get(threat_level, "high")

                cur.execute("""
                    INSERT INTO alerts
                        (alert_id, tenant_id, title, description, severity,
                         category, source_signal_id, status, metadata, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s, NOW())
                """, (
                    f"sec-{uuid.uuid4().hex[:12]}",
                    tenant_id,
                    title[:500],
                    description[:4000],
                    alert_severity,
                    classification,
                    str(signal_rid),
                    json.dumps({
                        "analysis": analysis,
                        "context": {
                            "executable": context.get("executable"),
                            "user": context.get("user"),
                            "source_ip": context.get("source_ip"),
                            "file_sha256": context.get("file_sha256"),
                        },
                        "node_id": node_id,
                    }),
                ))

            conn.commit()

        logger.info(
            "Security analysis stored: [%s] %s — %s (confidence: %s)",
            threat_level.upper(), classification, what_happened,
            analysis.get("confidence", "?"),
        )

    except Exception as e:
        logger.error("Failed to store security analysis: %s", e)


def _process_pending_events():
    """Find unanalyzed security signals and run LLM analysis on each."""
    from storage.postgres.db import get_connection

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                # Find security signals that haven't been analyzed yet
                analyzable = "', '".join(ANALYZABLE_TYPES)
                cur.execute(f"""
                    SELECT s.rid, s.signal_id, s.signal_type, s.payload,
                           s.severity, s.timestamp, s.tenant_id
                    FROM signals s
                    WHERE s.signal_type IN ('{analyzable}')
                      AND s.severity IN ('critical', 'high')
                      AND NOT EXISTS (
                          SELECT 1 FROM knowledge_nodes kn
                          WHERE kn.metadata->>'signal_rid' = s.rid::text
                            AND kn.metadata->>'security_analysis' = 'true'
                      )
                    ORDER BY s.timestamp DESC
                    LIMIT 5
                """)
                rows = cur.fetchall()

    except Exception as e:
        logger.debug("Failed to query security signals: %s", e)
        return

    for row in rows:
        signal_rid, signal_id, signal_type, payload, severity, timestamp, tenant_id = row

        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        payload["signal_type"] = signal_type
        payload["severity"] = severity
        payload["timestamp"] = timestamp.isoformat() if timestamp else "unknown"

        logger.info("Analyzing security event: %s (rid=%s)", signal_type, signal_rid)

        # 1. Gather context
        context = _gather_context(payload)

        # 2. LLM analysis
        analysis = _analyze_with_llm(context)
        if not analysis:
            logger.warning("LLM analysis returned nothing for rid=%s", signal_rid)
            continue

        # 3. Store assessment + alert
        _store_analysis(signal_rid, tenant_id or "default", context, analysis)


def _analyst_loop():
    """Main loop — poll for unanalyzed security events."""
    while not _stop_event.is_set():
        try:
            _process_pending_events()
        except Exception as e:
            logger.error("Security analyst error: %s", e)

        _stop_event.wait(POLL_INTERVAL)


def start_worker(tenant_id: str = "default"):
    """Start the security analyst worker thread."""
    global _thread

    if _thread and _thread.is_alive():
        return

    _stop_event.clear()
    _thread = threading.Thread(target=_analyst_loop, daemon=True, name="security-analyst")
    _thread.start()
    logger.info("Security analyst started (polling every %ds)", POLL_INTERVAL)


def stop_worker():
    """Stop the security analyst worker."""
    global _thread
    _stop_event.set()
    if _thread:
        _thread.join(timeout=5)
        _thread = None
    logger.info("Security analyst stopped")


def get_status() -> dict:
    """Return worker status."""
    return {
        "running": _thread is not None and _thread.is_alive(),
        "poll_interval": POLL_INTERVAL,
        "llm_model": LLM_MODEL,
        "analyzable_types": list(ANALYZABLE_TYPES),
    }
