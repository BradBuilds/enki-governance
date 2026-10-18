"""
Plaid Connector — real-time bank transactions from 12,000+ financial institutions.

Requires a Plaid access_token (obtained via Plaid Link widget or sandbox).
Polls for new transactions since last watermark. Each transaction becomes
a signal.financial.transaction signal.

Setup:
  1. Get API keys from https://plaid.com/
  2. Store as secrets: $secret:plaid-client-id, $secret:plaid-secret
  3. Use Plaid Link to generate access_token per institution
  4. Enable recipe with access_token

Config:
  {
    "client_id": "$secret:plaid-client-id",
    "secret": "$secret:plaid-secret",
    "access_token": "<institution-specific token>",
    "environment": "sandbox|development|production",
    "account_ids": ["optional", "filter", "list"]
  }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from storage.connectors.base import BaseConnector

logger = logging.getLogger("enki.connectors.plaid")


class PlaidConnector(BaseConnector):
    connector_type = "pull"
    source_type = "plaid_api"

    ENVS = {
        "sandbox": "https://sandbox.plaid.com",
        "development": "https://development.plaid.com",
        "production": "https://production.plaid.com",
    }

    def fetch(self) -> list[dict]:
        import urllib.request
        import urllib.error

        config = self.source_config
        client_id = config.get("client_id", "")
        secret = config.get("secret", "")
        access_token = config.get("access_token", "")
        env = config.get("environment", "sandbox")
        account_ids = config.get("account_ids") or []

        if not access_token:
            raise ValueError("Plaid access_token required")

        base_url = self.ENVS.get(env, self.ENVS["sandbox"])

        # Date range: last watermark to today
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_date = self.watermark.get(
            "start_date",
            (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
        )

        # Build request
        body = {
            "client_id": client_id,
            "secret": secret,
            "access_token": access_token,
            "start_date": start_date,
            "end_date": end_date,
            "options": {"count": 500, "offset": 0},
        }
        if account_ids:
            body["options"]["account_ids"] = (
                account_ids if isinstance(account_ids, list) else account_ids.split(",")
            )

        req = urllib.request.Request(
            f"{base_url}/transactions/get",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            raise RuntimeError(f"Plaid API error {e.code}: {error_body}")

        transactions = data.get("transactions", [])
        last_cursor = self.watermark.get("last_tx_id")

        items = []
        for tx in transactions:
            tx_id = tx.get("transaction_id", "")
            if last_cursor and tx_id <= last_cursor:
                continue

            payload = {
                "transaction_id": tx_id,
                "account_id": tx.get("account_id"),
                "amount": tx.get("amount", 0),
                "date": tx.get("date"),
                "merchant_name": tx.get("merchant_name") or tx.get("name", ""),
                "description": tx.get("name", ""),
                "category": tx.get("personal_finance_category", {}).get("primary", ""),
                "subcategory": tx.get("personal_finance_category", {}).get("detailed", ""),
                "pending": tx.get("pending", False),
                "iso_currency_code": tx.get("iso_currency_code", "USD"),
                "latitude": tx.get("location", {}).get("lat"),
                "longitude": tx.get("location", {}).get("lon"),
                "source": "plaid",
            }

            items.append({
                "raw_bytes": json.dumps(payload).encode("utf-8"),
                "content_type": "application/json",
                "source_type": "plaid_api",
            })

        logger.info("Plaid: fetched %d transactions (%s to %s)", len(items), start_date, end_date)
        return items

    def update_watermark(self, items: list[dict]) -> dict:
        wm = dict(self.watermark)
        wm["last_fetch_at"] = datetime.now(timezone.utc).isoformat()
        wm["start_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if items:
            try:
                first = json.loads(items[0]["raw_bytes"])
                wm["last_tx_id"] = first.get("transaction_id", "")
            except Exception:
                pass
        return wm
