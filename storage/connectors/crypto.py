"""
CoinGecko Crypto Connector — track cryptocurrency portfolio prices.

Free API, no key required. Polls for current prices and generates
signal.financial.balance_update signals for tracked coins.

Config:
  {
    "coin_ids": "bitcoin,ethereum,solana",
    "vs_currency": "usd"
  }
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from storage.connectors.base import BaseConnector

logger = logging.getLogger("enki.connectors.crypto")


class CryptoConnector(BaseConnector):
    connector_type = "pull"
    source_type = "coingecko_api"

    API_BASE = "https://api.coingecko.com/api/v3"

    def fetch(self) -> list[dict]:
        import urllib.request

        config = self.source_config
        coin_ids = config.get("coin_ids", "bitcoin")
        vs_currency = config.get("vs_currency", "usd")

        if isinstance(coin_ids, list):
            coin_ids = ",".join(coin_ids)

        url = (
            f"{self.API_BASE}/coins/markets"
            f"?vs_currency={vs_currency}"
            f"&ids={coin_ids}"
            f"&order=market_cap_desc"
            f"&sparkline=false"
            f"&price_change_percentage=24h,7d"
        )

        req = urllib.request.Request(url, headers={"Accept": "application/json"})

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                coins = json.loads(resp.read())
        except Exception as e:
            raise RuntimeError(f"CoinGecko API error: {e}")

        items = []
        for coin in coins:
            payload = {
                "coin_id": coin.get("id"),
                "symbol": coin.get("symbol", "").upper(),
                "name": coin.get("name"),
                "current_price": coin.get("current_price"),
                "market_cap": coin.get("market_cap"),
                "total_volume": coin.get("total_volume"),
                "price_change_24h": coin.get("price_change_percentage_24h"),
                "price_change_7d": coin.get("price_change_percentage_7d_in_currency"),
                "high_24h": coin.get("high_24h"),
                "low_24h": coin.get("low_24h"),
                "circulating_supply": coin.get("circulating_supply"),
                "ath": coin.get("ath"),
                "ath_change": coin.get("ath_change_percentage"),
                "currency": vs_currency.upper(),
                "source": "coingecko",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }

            items.append({
                "raw_bytes": json.dumps(payload).encode("utf-8"),
                "content_type": "application/json",
                "source_type": "coingecko_api",
            })

        logger.info("CoinGecko: fetched prices for %d coins", len(items))
        return items

    def update_watermark(self, items: list[dict]) -> dict:
        return {
            "last_fetch_at": datetime.now(timezone.utc).isoformat(),
            "coins_tracked": len(items),
        }
