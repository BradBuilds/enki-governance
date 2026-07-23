# Setup — Enki Governance

> Status: the stack and steps below are what runs in development. A fully hands-off
> one-command install is being validated on clean machines — if a step needs a tweak on
> your OS, please open an issue.

## Prerequisites
- **Docker** + **Docker Compose** (v2)
- ~**8 GB RAM** (more for large corpora)
- Optional **NVIDIA GPU** for a faster local model (CPU works, just slower)
- No external accounts or API keys required for the core

## 1. Clone and configure
```bash
git clone https://github.com/BradBuilds/enki-governance.git
cd enki-governance
cp .env.example .env
```
The defaults in `.env` work for local development. The one you may care about:
```dotenv
OLLAMA_MODEL=gemma4:e2b        # the local model; any Ollama model works
```

## 2. Bring up the stack
```bash
docker compose up -d           # postgres (PostGIS) + ollama + api + ui
```
First run pulls images and builds — give it a few minutes.

## 3. Initialize (migrations, model, packs)
```bash
./scripts/first-run.sh
```
This:
1. waits for Postgres,
2. applies the schema migrations (`storage/postgres/migrations/`),
3. pulls your local model into Ollama (first pull is slow),
4. restarts the API so it loads the **governance packs** (the `libraries/` spine).

You can also run these steps by hand — see the script; it's short and readable.

## 4. Use it
- Open the UI (default **http://localhost:5173**).
- Upload a PDF or text document.
- Watch it flow: **ingest → extract → governed collapse → sourced fact graph.** Open any
  entity to see its typed, sourced relationships; click a receipt to jump to the exact
  page it came from.

## Optional: WordNet sense layer

All 47 governance packs ship, but the two WordNet sense packs need their large data built
from [Open English WordNet](https://github.com/globalwordnet/english-wordnet) (`oewn:2023`,
CC-BY-4.0). Enable sense-grounding (word-sense disambiguation) with:

```bash
./scripts/import-lexicon.sh
```

Heavy (downloads OEWN ~35 MB, builds ~120k senses) and needs network. Core collapse works
without it. Via the `wn` library you can also pull other/multilingual wordnets for coverage.

## Configuration reference
See `.env.example` — every setting is documented inline. Key groups: database, Ollama
(local model), and API/UI ports.

## Troubleshooting
- **Model won't pull / OOM:** pick a smaller model in `.env` (`OLLAMA_MODEL`).
- **Empty graph after upload:** collapse runs after extraction; give it a minute, and check
  `docker compose logs -f api`. Sparse results usually mean the packs don't cover that
  vocabulary yet — that's the invitation to [contribute an edge](CONTRIBUTING.md).
- **Ports in use:** override them in `.env`.
