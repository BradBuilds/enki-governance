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

## A note on the sense layer

The word-sense layer is not shipped. Open English WordNet is CC-BY-4.0, and this project's
data policy is public-domain only — a policy about **use**, not just redistribution, since
validating against a resource is using it. Publishing the converters would invite downstream
nodes to build a CC-BY substrate and federate it onward, carrying an attribution obligation
into other people's corpora. See the README for the full reasoning.

Core collapse does not need it. Entity resolution, typed relationships, receipts and the
governance chain all work without a sense layer, and the domain glossaries ship in the packs.

## Optional: sign your pack set

Enki can prove its governance packs haven't been altered. Every pack is content-hashed,
`active_set_v6.json` pins those hashes, and a signature commits to the active set plus the
transcript of the admission ceremony that verified it. Change any pack byte and the
signature stops verifying.

You don't need this to run Enki — the default `node-local` profile doesn't ask for it. You
do need it for the `node`, `node-public`, `evidentiary`, `cloud`, `hardware`, and
`hardened` profiles, which refuse to boot without a valid signature.

One command sets up the whole chain:

```bash
python scripts/governance/setup_governance.py
```

It creates a trust root and a signer key (both Ed25519, sealed with AES-256-GCM under a
password you choose), runs the admission ceremony over all packs, signs the active set, and
then verifies the result using the exact check the server runs at boot. It's safe to re-run
— finished steps are skipped.

**Re-run it after changing any pack.** Editing a pack changes its hash, which invalidates
the signature by design:

```bash
python scripts/governance/setup_governance.py    # re-runs the ceremony and re-signs
```

What you end up with:

| Path | What it is |
|---|---|
| `storage/keys/root_key.enc` | Tier-R root key, sealed |
| `storage/keys/signer_key.enc` | Tier-K signer key, sealed |
| `storage/grants/*.json` | the grant chain (root → signer) |
| `/etc/enki/trusted_keys.signed` | the root pubkey the verifier trusts |
| `active_set_v6.signed.json` | the signature itself |

Only the last file is read at boot; the rest is what proves it. **Back up the sealed keys
and the password.** Losing the signer means re-issuing one and re-signing; losing the root
means rebuilding the trust root from scratch, and everything signed under the old root
stops verifying.

Hardware signing (a USB key that holds the private key and requires a physical button press
per signature) is also supported for installs that want it — pass `--device /dev/ttyACM0` to
`scripts/governance/sign_active_set.py` instead of `--signer-key`. A software key can be
copied by anything that can read the file and the password; a hardware key can't. For a
personal or team node the software key is the right trade, and it's the default.

## Configuration reference
See `.env.example` — every setting is documented inline. Key groups: database, Ollama
(local model), and API/UI ports.

## Troubleshooting
- **Model won't pull / OOM:** pick a smaller model in `.env` (`OLLAMA_MODEL`).
- **Empty graph after upload:** collapse runs after extraction; give it a minute, and check
  `docker compose logs -f api`. Sparse results usually mean the packs don't cover that
  vocabulary yet — that's the invitation to [contribute an edge](CONTRIBUTING.md).
- **Ports in use:** override them in `.env`.
