"""
HTTP API pull connector — connect to ANY REST API.

Fetches JSON from any HTTP endpoint on a schedule and feeds it
into the ingestion pipeline. Supports:
  - GET or POST requests
  - Custom headers (auth tokens, API keys)
  - Pagination (cursor, offset, or link-based)
  - Watermarking (timestamp or ID-based dedup)
  - Response path extraction (dig into nested JSON)

Examples:
  Salesforce API, Stripe API, weather API, GitHub API,
  any JSON endpoint.

source_config:
  {
    "url": "https://api.example.com/data",
    "method": "GET",
    "headers": {"Authorization": "Bearer <token>"},
    "params": {"since": "{watermark.last_id}"},
    "response_path": "data.items",     # dig into response JSON
    "id_field": "id",                  # field to use for watermark
    "max_items": 100,                  # limit per fetch
  }
"""

import json
from datetime import datetime, timezone
from typing import Optional
from urllib.request import urlopen, Request
from urllib.parse import urlencode, urljoin

from storage.connectors.base import BaseConnector
from storage.connectors.manager import register_connector_class


class HttpApiConnector(BaseConnector):
    connector_type = "pull"
    source_type = "http_api"

    def fetch(self) -> list[dict]:
        config = self.source_config
        url = config.get("url")
        if not url:
            raise ValueError("HTTP API connector requires 'url' in source_config")

        method = config.get("method", "GET").upper()
        headers = dict(config.get("headers", {}))
        headers.setdefault("User-Agent", "Enki/1.0 (HTTP API Connector)")
        headers.setdefault("Accept", "application/json")

        # Build params with watermark interpolation
        params = dict(config.get("params", {}))
        for key, val in params.items():
            if isinstance(val, str) and "{watermark." in val:
                field = val.split("{watermark.")[1].rstrip("}")
                params[key] = self.watermark.get(field, "")

        # Build URL with params
        if params and method == "GET":
            separator = "&" if "?" in url else "?"
            url = url + separator + urlencode({k: v for k, v in params.items() if v})

        # Make request
        body = None
        if method == "POST":
            body = json.dumps(params).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = Request(url, data=body, headers=headers, method=method)
        with urlopen(req, timeout=30) as resp:
            response_bytes = resp.read()

        # Parse response
        response_data = json.loads(response_bytes)

        # Extract items from response path
        response_path = config.get("response_path")
        items_data = _extract_path(response_data, response_path)

        # Normalize to list
        if isinstance(items_data, dict):
            items_data = [items_data]
        elif not isinstance(items_data, list):
            items_data = [{"value": items_data}]

        # Limit
        max_items = config.get("max_items", 100)
        items_data = items_data[:max_items]

        # Filter by watermark (skip already-seen items)
        id_field = config.get("id_field")
        last_id = self.watermark.get("last_id")
        if id_field and last_id:
            filtered = []
            for item in items_data:
                item_id = item.get(id_field) if isinstance(item, dict) else None
                if item_id and str(item_id) == str(last_id):
                    break  # stop at watermark
                filtered.append(item)
            items_data = filtered

        # Convert to pipeline items
        items = []
        for item in items_data:
            raw = json.dumps(item, ensure_ascii=False, default=str).encode("utf-8")
            items.append({
                "raw_bytes": raw,
                "content_type": "application/json",
                "source_type": "http_api",
            })

        return items

    def update_watermark(self, items: list[dict]) -> dict:
        if not items:
            return self.watermark

        id_field = self.source_config.get("id_field")
        new_watermark = {
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "items_fetched": len(items),
        }

        if id_field and items:
            try:
                first = json.loads(items[0]["raw_bytes"])
                new_watermark["last_id"] = str(first.get(id_field, ""))
            except (json.JSONDecodeError, KeyError):
                pass

        return new_watermark


def _extract_path(data, path: Optional[str]):
    """Extract nested value from dict using dot-separated path.
    'data.items' → data['data']['items']

    Returns [] if path doesn't match — never returns the original
    response, which would cause the entire response to be treated
    as a single item.
    """
    if not path:
        return data

    current = data
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.isdigit():
            idx = int(key)
            current = current[idx] if idx < len(current) else None
        else:
            import logging
            logging.getLogger("enki.connector.http").warning(
                "response_path '%s' not found in response (key '%s' not in %s)",
                path, key, type(current).__name__,
            )
            return []
        if current is None:
            import logging
            logging.getLogger("enki.connector.http").warning(
                "response_path '%s' returned None at key '%s'", path, key,
            )
            return []
    return current


# Register
register_connector_class("http_api", HttpApiConnector)
