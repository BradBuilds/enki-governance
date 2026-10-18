"""
Generic REST API connector handler.

For sources that publish JSON via a stable endpoint. The handler
fetches the URL with the configured params/body, then projects each
item into the standard yield shape.

Config keys:
    base_url        (required)
    endpoint        (required)
    method          'GET' | 'POST'   default 'GET'
    params          dict — GET querystring
    body_template   dict — POST body, with {{today}}/{{yesterday}}
                    substitution
    requires_api_key   bool — gate
    api_key_env     env var holding the key (passed as X-Api-Key)
    item_path       dot-path to the items list in the response
                    (e.g., "results" / "data.items")
    fields          dict mapping yield-keys → response paths:
                    {"source_url": "url",
                     "source_title": "title",
                     "body": "summary",
                     "item_id": "id",
                     "published_at": "publishedAt"}
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

log = logging.getLogger("enki.connectors.rest_api")

DEFAULT_UA = "ENKI-rest-connector/1.0 (https://example.com)"


def _substitute(template: dict | str, now: datetime) -> dict | str:
    """Replace {{today}} / {{yesterday}} in strings."""
    today = now.date().isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()
    if isinstance(template, str):
        return (template
                .replace("{{today}}", today)
                .replace("{{yesterday}}", yesterday))
    if isinstance(template, dict):
        return {k: _substitute(v, now) for k, v in template.items()}
    if isinstance(template, list):
        return [_substitute(v, now) for v in template]
    return template


def _pick(obj: dict, path: str):
    """Get a value via dot-path. Returns None if any step missing."""
    if not path:
        return None
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def fetch_recent(
    config: dict,
    *,
    since: Optional[datetime] = None,
) -> Iterator[dict]:
    base_url = config.get("base_url")
    endpoint = config.get("endpoint", "")
    method = (config.get("method") or "GET").upper()
    if not base_url:
        return

    # API key gate
    if config.get("requires_api_key"):
        env_var = config.get("api_key_env") or "API_KEY"
        if not os.environ.get(env_var):
            log.info("rest_api connector skipping — %s not set", env_var)
            return

    now = datetime.now(timezone.utc)

    url = base_url.rstrip("/") + endpoint
    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json",
    }
    if config.get("requires_api_key"):
        headers["X-Api-Key"] = os.environ[config["api_key_env"]]

    body = None
    if method == "GET":
        params = _substitute(config.get("params") or {}, now)
        if params:
            url += "?" + urllib.parse.urlencode(params)
    elif method == "POST":
        body_template = _substitute(config.get("body_template") or {}, now)
        body = json.dumps(body_template).encode("utf-8")
        headers["Content-Type"] = "application/json"

    try:
        req = urllib.request.Request(url, headers=headers, data=body, method=method)
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        log.warning("rest_api fetch failed for %s: %s", url, e)
        return

    item_path = config.get("item_path", "")
    items = _pick(payload, item_path) if item_path else payload
    if not isinstance(items, list):
        # Try common shapes
        items = (payload.get("results") if isinstance(payload, dict) else None) \
                or (payload.get("data") if isinstance(payload, dict) else None) \
                or []
    if not isinstance(items, list):
        log.debug("rest_api: no list of items found in response")
        return

    fields = config.get("fields") or {}

    for item in items:
        if not isinstance(item, dict):
            continue
        src_url = _pick(item, fields.get("source_url") or "url")
        if not src_url:
            continue
        title = _pick(item, fields.get("source_title") or "title") or "(untitled)"
        body_text = _pick(item, fields.get("body") or "summary")
        if not body_text:
            # If no body field configured, serialize the whole item.
            body_text = json.dumps(item, indent=2)

        item_id = _pick(item, fields.get("item_id") or "id") or str(src_url)
        published_raw = _pick(item, fields.get("published_at") or "publishedAt")
        published_at = None
        if published_raw:
            try:
                published_at = datetime.fromisoformat(
                    str(published_raw).replace("Z", "+00:00"))
            except Exception:
                pass

        if since and published_at and published_at <= since:
            continue

        yield {
            "body": str(body_text).encode("utf-8"),
            "source_url": str(src_url)[:2000],
            "source_title": str(title)[:500],
            "mime_type": "application/json",
            "item_id": str(item_id),
            "published_at": published_at.isoformat() if published_at else None,
        }
