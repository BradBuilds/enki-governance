<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Connectors — pull live news and APIs into the collapse engine

Enki isn't only a "drop a folder of PDFs" tool. It ships a small connector framework so
you can point it at **live sources** — news feeds, JSON APIs, a watched folder — and it
polls them on a schedule, ingesting each new item and collapsing it into the same governed,
sourced graph as any other document. All of it is included in this repo and runs locally.

> **What "offline" means with connectors on.** The engine still runs entirely on your
> machine and **never sends your data out**. Connectors are *opt-in* and only make
> **outbound fetches to pull public sources in** (an RSS URL, an API endpoint). If you want a
> truly air-gapped deployment, simply don't configure any connectors — document ingestion and
> the whole governed spine work with no network at all.

## The connectors that ship

| `source_type` | What it does | Key config |
|---|---|---|
| `rss_feed` | Polls an RSS/Atom feed; each new entry becomes a document. Pure stdlib XML — no `feedparser` dependency. | `url` (required); `body_strategy`: `summary` \| `content` \| `fetch_link` (default `summary`); `user_agent`; `body_max_bytes` |
| `http_api` | Any source that returns JSON over a stable endpoint. Maps response fields into documents. | `base_url`, `endpoint`, `method` (`GET`/`POST`), `params`, `item_path` (dot-path to the list, e.g. `data.items`), `fields` (map yield-keys → response paths), optional `api_key_env` |
| `file_drop` | Watches `/data/inbox/` and ingests anything dropped in — PDF, CSV, email, DOCX/XLSX, HTML, images, and ZIP/TAR/MBOX archives (incl. Google Takeout / Apple Privacy exports). Files move to `/data/processed/` or `/data/failed/`. | `ENKI_INBOX_DIR` (default `/data/inbox`) |

There is also a built-in **GDELT** sync (`core/connectors/gdelt_gkg.py`) — the open
[GDELT](https://www.gdeltproject.org/) global knowledge graph of news events, geolocated for
the map layer. It runs on its own refresh, independent of the user connectors above.

## How polling works

A single background **scheduler** thread starts with the API (`core/connectors/scheduler.py`).
Every 30 seconds it checks each active connector and, if `last_fetch_at + poll_interval < now`,
triggers a fetch. Each fetched item flows through the normal pipeline — extract → **governed
collapse** → sourced graph — so a headline pulled from RSS is collapsed and receipted exactly
like a PDF you dropped in.

## Add one from the dashboard

Open the UI → **Connectors** (or **Settings → Data sources**), choose a source type, fill in
the config, and hit **Test connection** before saving. The **Feed** view shows items as they
arrive; **Dashboard** rolls up the graph they build.

## Add one from the API

```bash
# Subscribe to an RSS feed, polled every 15 minutes
curl -X POST localhost:8000/connectors \
  -H 'Content-Type: application/json' \
  -d '{
    "source_type": "rss_feed",
    "poll_interval": 900,
    "source_config": { "url": "https://example.com/feed.xml", "body_strategy": "fetch_link" }
  }'

# A JSON API, projecting fields into documents
curl -X POST localhost:8000/connectors \
  -H 'Content-Type: application/json' \
  -d '{
    "source_type": "http_api",
    "poll_interval": 3600,
    "source_config": {
      "base_url": "https://api.example.com",
      "endpoint": "/v1/articles",
      "item_path": "results",
      "fields": { "source_url": "url", "source_title": "title",
                  "body": "summary", "item_id": "id", "published_at": "publishedAt" }
    }
  }'
```

Other endpoints: `GET /connectors` (list), `GET /connectors/{id}`, `POST /connectors/{id}/fetch`
(trigger now), `POST /connectors/test-connection`, `POST /connectors/upload`.

## Worked example — one feed, collapsed

1. Add an `rss_feed` connector pointing at any news/blog feed (above).
2. `POST /connectors/{id}/fetch` — or wait for the scheduler.
3. Watch the **Feed**: each entry becomes a document, is extracted, and **collapses** into
   entities and typed, sourced relationships. Open any resulting entity to see the receipt —
   the exact feed item the claim came from.

## What's *not* here

Paid/commercial data connectors and the surveillance/tracking connectors (device, camera,
fleet, geofence feeds) are **not** part of this open repo — see the README's *Limitations*.
The connectors above are free/open sources you configure yourself.

## Add your own

A connector is just a handler that yields `ingest_document`-shaped dicts. `rss.py` (~stdlib
only) is the simplest template. New source types are a natural contribution — and, like the
packs, the useful ones are worth upstreaming.
