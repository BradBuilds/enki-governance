"""
MQTT connector — subscribe to MQTT topics and ingest messages.

Uses a pull-based approach: connects, grabs queued messages, disconnects.
For persistent streaming, use the worker system to call fetch() on a schedule.

Note: Requires paho-mqtt (pip install paho-mqtt). If not installed,
the connector won't register but won't crash the system.

source_config:
  {
    "broker": "mqtt.example.com",
    "port": 1883,
    "topics": ["sensors/#", "gps/+/position"],
    "username": "enki",
    "password": "secret",
    "use_tls": false,
    "client_id": "enki-connector",
    "qos": 1,
    "max_messages": 100,
    "timeout": 10
  }
"""

import json
import time
import threading
from datetime import datetime, timezone
from typing import Optional

from storage.connectors.base import BaseConnector
from storage.connectors.manager import register_connector_class


class MqttConnector(BaseConnector):
    connector_type = "pull"  # pull-style: connect, grab messages, disconnect
    source_type = "mqtt"

    def fetch(self) -> list[dict]:
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            raise RuntimeError("paho-mqtt not installed. Run: pip install paho-mqtt")

        config = self.source_config
        broker = config.get("broker")
        port = config.get("port", 1883)
        topics = config.get("topics", ["#"])
        username = config.get("username")
        password = config.get("password")
        use_tls = config.get("use_tls", False)
        client_id = config.get("client_id", f"enki-{self.connector_id}")
        qos = config.get("qos", 1)
        max_messages = config.get("max_messages", 100)
        timeout = config.get("timeout", 10)

        if not broker:
            raise ValueError("MQTT connector requires 'broker' in source_config")

        collected: list[dict] = []
        done = threading.Event()

        def on_connect(client, userdata, flags, rc):
            if rc != 0:
                done.set()
                return
            for topic in topics:
                client.subscribe(topic, qos=qos)

        def on_message(client, userdata, msg):
            if len(collected) >= max_messages:
                done.set()
                return

            # Build a structured payload with topic metadata
            try:
                payload_str = msg.payload.decode("utf-8", errors="replace")
                # Try to parse as JSON
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    payload = {"raw_value": payload_str}

                payload["_mqtt_topic"] = msg.topic
                payload["_mqtt_qos"] = msg.qos
                payload["_mqtt_retain"] = msg.retain

                raw = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
                collected.append({
                    "raw_bytes": raw,
                    "content_type": "application/json",
                    "source_type": "mqtt",
                })
            except Exception:
                # Store raw bytes if all else fails
                collected.append({
                    "raw_bytes": msg.payload,
                    "content_type": "application/octet-stream",
                    "source_type": "mqtt",
                })

        client = mqtt.Client(client_id=client_id)

        if username:
            client.username_pw_set(username, password)
        if use_tls:
            client.tls_set()

        client.on_connect = on_connect
        client.on_message = on_message

        try:
            client.connect(broker, port, keepalive=timeout + 5)
            client.loop_start()

            # Wait for messages or timeout
            done.wait(timeout=timeout)

            client.loop_stop()
            client.disconnect()
        except Exception as e:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
            raise RuntimeError(f"MQTT connection failed: {e}")

        return collected

    def update_watermark(self, items: list[dict]) -> dict:
        return {
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "items_fetched": len(items),
            "message_count": self.watermark.get("message_count", 0) + len(items),
        }


# Register (only if paho-mqtt is installed)
try:
    import paho.mqtt.client  # noqa: F401
    register_connector_class("mqtt", MqttConnector)
except ImportError:
    pass
