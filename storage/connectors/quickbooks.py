"""
QuickBooks Online Connector — sync transactions, invoices, and accounts.

Uses QuickBooks API with OAuth2 access token. Polls for recent transactions
and converts them to signal.financial.transaction signals.

Setup:
  1. Register app at https://developer.intuit.com/
  2. Complete OAuth2 flow to get access_token + refresh_token
  3. Store as secrets: $secret:qbo-access-token, $secret:qbo-refresh-token
  4. Enable recipe with company_id

Config:
  {
    "access_token": "$secret:qbo-access-token",
    "refresh_token": "$secret:qbo-refresh-token",
    "company_id": "123456789",
    "environment": "sandbox|production"
  }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from storage.connectors.base import BaseConnector

logger = logging.getLogger("enki.connectors.quickbooks")


class QuickBooksConnector(BaseConnector):
    connector_type = "pull"
    source_type = "quickbooks_api"

    ENVS = {
        "sandbox": "https://sandbox-quickbooks.api.intuit.com",
        "production": "https://quickbooks.api.intuit.com",
    }

    def fetch(self) -> list[dict]:
        import urllib.request
        import urllib.error

        config = self.source_config
        access_token = config.get("access_token", "")
        company_id = config.get("company_id", "")
        env = config.get("environment", "sandbox")

        if not access_token or not company_id:
            raise ValueError("QuickBooks access_token and company_id required")

        base_url = self.ENVS.get(env, self.ENVS["sandbox"])

        # Query recent purchases/expenses
        last_sync = self.watermark.get(
            "last_sync",
            (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
        )

        query = (
            f"SELECT * FROM Purchase WHERE MetaData.LastUpdatedTime > '{last_sync}' "
            f"ORDERBY MetaData.LastUpdatedTime DESC MAXRESULTS 500"
        )

        req = urllib.request.Request(
            f"{base_url}/v3/company/{company_id}/query?query={query}",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode() if e.fp else str(e)
            raise RuntimeError(f"QuickBooks API error {e.code}: {error_body}")

        purchases = (
            data.get("QueryResponse", {}).get("Purchase", [])
        )

        items = []
        for p in purchases:
            total = float(p.get("TotalAmt", 0))
            lines = p.get("Line", [])
            description = lines[0].get("Description", "") if lines else ""

            payload = {
                "transaction_id": p.get("Id", ""),
                "date": p.get("TxnDate", ""),
                "amount": -total,  # Purchases are outflows
                "merchant_name": p.get("EntityRef", {}).get("name", ""),
                "description": description or p.get("PrivateNote", ""),
                "account_id": p.get("AccountRef", {}).get("value", ""),
                "account_name": p.get("AccountRef", {}).get("name", ""),
                "payment_type": p.get("PaymentType", ""),
                "currency": p.get("CurrencyRef", {}).get("value", "USD"),
                "source": "quickbooks",
            }

            items.append({
                "raw_bytes": json.dumps(payload).encode("utf-8"),
                "content_type": "application/json",
                "source_type": "quickbooks_api",
            })

        logger.info("QuickBooks: fetched %d transactions since %s", len(items), last_sync)
        return items

    def update_watermark(self, items: list[dict]) -> dict:
        return {
            "last_sync": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "items_fetched": len(items),
        }
