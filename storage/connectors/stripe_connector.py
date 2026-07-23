"""
Stripe Connector — payment events, charges, disputes, and payouts.

Polls Stripe API for recent charges/events. Each charge becomes a
signal.financial.transaction signal. Disputes and payouts become alerts.

Config:
  {
    "api_key": "$secret:stripe-api-key",
    "event_types": "charge.succeeded,charge.refunded,payout.paid,dispute.created"
  }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from storage.connectors.base import BaseConnector

logger = logging.getLogger("enki.connectors.stripe")


class StripeConnector(BaseConnector):
    connector_type = "pull"
    source_type = "stripe_api"

    API_BASE = "https://api.stripe.com/v1"

    def fetch(self) -> list[dict]:
        import urllib.request
        import urllib.parse

        config = self.source_config
        api_key = config.get("api_key", "")

        if not api_key:
            raise ValueError("Stripe API key required")

        # Fetch recent events since watermark
        params = {
            "limit": "100",
        }
        event_types = config.get("event_types", "charge.succeeded,charge.refunded,payout.paid")
        if event_types:
            for i, t in enumerate(event_types.split(",")):
                params[f"types[]"] = t.strip()

        last_event = self.watermark.get("last_event_id")
        if last_event:
            params["starting_after"] = last_event

        url = f"{self.API_BASE}/events?{urllib.parse.urlencode(params, doseq=True)}"

        import base64
        auth = base64.b64encode(f"{api_key}:".encode()).decode()

        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Basic {auth}",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            raise RuntimeError(f"Stripe API error: {e}")

        events = data.get("data", [])

        items = []
        for event in events:
            obj = event.get("data", {}).get("object", {})
            event_type = event.get("type", "")

            if "charge" in event_type:
                payload = {
                    "transaction_id": obj.get("id"),
                    "amount": obj.get("amount", 0) / 100,  # cents to dollars
                    "currency": (obj.get("currency") or "usd").upper(),
                    "description": obj.get("description") or obj.get("statement_descriptor", ""),
                    "merchant_name": obj.get("billing_details", {}).get("name", ""),
                    "date": datetime.fromtimestamp(
                        obj.get("created", 0), tz=timezone.utc
                    ).strftime("%Y-%m-%d"),
                    "status": obj.get("status"),
                    "event_type": event_type,
                    "source": "stripe",
                }
            elif "payout" in event_type:
                payload = {
                    "transaction_id": obj.get("id"),
                    "amount": obj.get("amount", 0) / 100,
                    "currency": (obj.get("currency") or "usd").upper(),
                    "description": f"Stripe payout",
                    "date": datetime.fromtimestamp(
                        obj.get("arrival_date", 0), tz=timezone.utc
                    ).strftime("%Y-%m-%d"),
                    "status": obj.get("status"),
                    "event_type": event_type,
                    "source": "stripe",
                }
            elif "dispute" in event_type:
                payload = {
                    "transaction_id": obj.get("id"),
                    "amount": obj.get("amount", 0) / 100,
                    "currency": (obj.get("currency") or "usd").upper(),
                    "description": f"Dispute: {obj.get('reason', 'unknown')}",
                    "date": datetime.fromtimestamp(
                        obj.get("created", 0), tz=timezone.utc
                    ).strftime("%Y-%m-%d"),
                    "status": obj.get("status"),
                    "event_type": event_type,
                    "source": "stripe",
                }
            else:
                continue

            items.append({
                "raw_bytes": json.dumps(payload).encode("utf-8"),
                "content_type": "application/json",
                "source_type": "stripe_api",
            })

        logger.info("Stripe: fetched %d events", len(items))
        return items

    def update_watermark(self, items: list[dict]) -> dict:
        wm = dict(self.watermark)
        wm["last_fetch_at"] = datetime.now(timezone.utc).isoformat()
        # Stripe events are returned newest-first, so last item = oldest = our cursor
        if items:
            try:
                last = json.loads(items[-1]["raw_bytes"])
                wm["last_event_id"] = last.get("transaction_id", "")
            except Exception:
                pass
        return wm
