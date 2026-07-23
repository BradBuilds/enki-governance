"""
Code generation templates for the AI Connect Wizard.

Templates are safe f-string patterns — the LLM chooses WHICH template
and fills in parameters, but never generates arbitrary code.

Every template produces a working integration that POSTs data to
the Enki signal ingestion endpoint.
"""

from __future__ import annotations


def render_template(template_id: str, **kwargs) -> dict:
    """Render a code template with the given parameters.

    Returns:
        {"language": "bash", "code": "...", "filename": "enki_push.sh", "description": "..."}
    """
    tmpl = TEMPLATES.get(template_id)
    if not tmpl:
        return {"error": f"Unknown template: {template_id}"}

    try:
        code = tmpl["code"].format(**{
            "server_url": kwargs.get("server_url", "https://example.com"),
            "device_id": kwargs.get("device_id", "my-device"),
            "tenant_id": kwargs.get("tenant_id", "default"),
            "signal_type": kwargs.get("signal_type", ""),
            "sample_payload": kwargs.get("sample_payload", '{"value": 1}'),
            "poll_url": kwargs.get("poll_url", "https://api.example.com/data"),
            "poll_interval": kwargs.get("poll_interval", "300"),
            "api_key": kwargs.get("api_key", "YOUR_API_KEY"),
            "webhook_path": kwargs.get("webhook_path", "my-device"),
        })
        return {
            "language": tmpl["language"],
            "code": code,
            "filename": tmpl["filename"],
            "description": tmpl["description"],
            "template_id": template_id,
        }
    except KeyError as e:
        return {"error": f"Missing template variable: {e}"}


# ─── Templates ───────────────────────────────────────────────────────────────

TEMPLATES = {
    # ── Push: one-shot curl ──────────────────────────────────────────────
    "push_curl": {
        "language": "bash",
        "filename": "enki_push.sh",
        "description": "Push a single signal via curl",
        "code": """#!/bin/bash
# Push a signal to Enki Governance
# Usage: bash enki_push.sh

curl -X POST {server_url}/signals/ingest \\
  -H "X-Device-ID: {device_id}" \\
  -H "X-Tenant-ID: {tenant_id}" \\
  -H "Content-Type: application/json" \\
  -d '{sample_payload}'
""",
    },

    # ── Push: Python script ──────────────────────────────────────────────
    "push_python": {
        "language": "python",
        "filename": "enki_push.py",
        "description": "Push signals from Python (sensor, script, etc.)",
        "code": """#!/usr/bin/env python3
\"\"\"Push signals to Enki Governance.\"\"\"
import requests
import json
from datetime import datetime, timezone

SERVER = "{server_url}"
DEVICE_ID = "{device_id}"
TENANT_ID = "{tenant_id}"

def send_signal(data: dict):
    \"\"\"Send a signal to Enki. Returns the signal_id on success.\"\"\"
    data["timestamp"] = datetime.now(timezone.utc).isoformat()
    resp = requests.post(
        f"{{SERVER}}/signals/ingest",
        headers={{
            "X-Device-ID": DEVICE_ID,
            "X-Tenant-ID": TENANT_ID,
            "Content-Type": "application/json",
        }},
        json=data,
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()
    print(f"Signal accepted: {{result.get('signal_id')}} ({{result.get('signal_type')}})")
    return result

if __name__ == "__main__":
    # Example: send your data
    send_signal({sample_payload})
""",
    },

    # ── Push: Python with polling loop ───────────────────────────────────
    "push_python_loop": {
        "language": "python",
        "filename": "enki_collector.py",
        "description": "Continuously collect and push data on an interval",
        "code": """#!/usr/bin/env python3
\"\"\"Collect data and push to Enki on an interval.\"\"\"
import requests
import time
import json
from datetime import datetime, timezone

SERVER = "{server_url}"
DEVICE_ID = "{device_id}"
TENANT_ID = "{tenant_id}"
POLL_INTERVAL = {poll_interval}  # seconds

def collect_data():
    \"\"\"Replace this with your actual data collection logic.\"\"\"
    # Example: read from sensor, API, file, etc.
    return {sample_payload}

def send_signal(data: dict):
    data["timestamp"] = datetime.now(timezone.utc).isoformat()
    try:
        resp = requests.post(
            f"{{SERVER}}/signals/ingest",
            headers={{
                "X-Device-ID": DEVICE_ID,
                "X-Tenant-ID": TENANT_ID,
                "Content-Type": "application/json",
            }},
            json=data,
            timeout=10,
        )
        result = resp.json()
        print(f"[{{datetime.now():%H:%M:%S}}] Sent: {{result.get('signal_type', 'unknown')}} -> {{result.get('path', '?')}}")
    except Exception as e:
        print(f"[{{datetime.now():%H:%M:%S}}] Error: {{e}}")

if __name__ == "__main__":
    print(f"Enki collector started. Sending every {{POLL_INTERVAL}}s to {{SERVER}}")
    while True:
        data = collect_data()
        send_signal(data)
        time.sleep(POLL_INTERVAL)
""",
    },

    # ── Push: Node.js ────────────────────────────────────────────────────
    "push_node": {
        "language": "javascript",
        "filename": "enki_push.js",
        "description": "Push signals from Node.js",
        "code": """// Push a signal to Enki Governance
const SERVER = "{server_url}";
const DEVICE_ID = "{device_id}";
const TENANT_ID = "{tenant_id}";

async function sendSignal(data) {{
  data.timestamp = new Date().toISOString();
  const resp = await fetch(`${{SERVER}}/signals/ingest`, {{
    method: "POST",
    headers: {{
      "X-Device-ID": DEVICE_ID,
      "X-Tenant-ID": TENANT_ID,
      "Content-Type": "application/json",
    }},
    body: JSON.stringify(data),
  }});
  const result = await resp.json();
  console.log(`Signal accepted: ${{result.signal_id}} (${{result.signal_type}})`);
  return result;
}}

// Example: send your data
sendSignal({sample_payload});
""",
    },

    # ── Cron: periodic fetch + push ──────────────────────────────────────
    "cron_curl": {
        "language": "bash",
        "filename": "enki_cron.sh",
        "description": "Cron job: fetch from external API, push to Enki",
        "code": """#!/bin/bash
# Fetch data from an external source and push to Enki
# Install: crontab -e
# Add:  */{poll_interval} * * * * /path/to/enki_cron.sh

DATA=$(curl -s "{poll_url}")

curl -s -X POST {server_url}/signals/ingest \\
  -H "X-Device-ID: {device_id}" \\
  -H "X-Tenant-ID: {tenant_id}" \\
  -H "Content-Type: application/json" \\
  -d "$DATA"
""",
    },

    # ── Webhook: device config instructions ──────────────────────────────
    "webhook_config": {
        "language": "text",
        "filename": "webhook_setup.txt",
        "description": "Configure your device to POST to this webhook URL",
        "code": """WEBHOOK CONFIGURATION
====================

Configure your device to send HTTP POST requests to:

  URL:  {server_url}/webhooks/generic/{webhook_path}

Headers (optional):
  Content-Type: application/json   (or application/xml, image/jpeg, etc.)
  X-Tenant-ID: {tenant_id}

The server accepts ANY format — JSON, XML, images, binary.
Data is automatically classified and stored.

Example test with curl:
  curl -X POST {server_url}/webhooks/generic/{webhook_path} \\
    -H "Content-Type: application/json" \\
    -d '{sample_payload}'
""",
    },

    # ── Pull: connector config JSON ──────────────────────────────────────
    "connector_config": {
        "language": "json",
        "filename": "connector.json",
        "description": "Connector configuration (created automatically)",
        "code": """{{
  "device_id": "{device_id}",
  "connector_type": "pull",
  "source_type": "http_api",
  "poll_interval": "{poll_interval} seconds",
  "source_config": {{
    "url": "{poll_url}",
    "method": "GET",
    "headers": {{}},
    "response_path": "",
    "id_field": "id"
  }}
}}""",
    },
}


def list_templates() -> list[dict]:
    """Return a summary of all available templates."""
    return [
        {"id": tid, "language": t["language"], "description": t["description"]}
        for tid, t in TEMPLATES.items()
    ]
