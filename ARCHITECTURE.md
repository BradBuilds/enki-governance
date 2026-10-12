<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Architecture — Enki Governance

Enki Governance turns documents into a **governed, sourced fact graph**. This document
explains the model so you can trust — and extend — what it does.

## The one-sentence model

```
documents ─► extract statements ─► GOVERNED COLLAPSE ─► entity + fact graph
                                        │                    (every edge sourced)
                                  packs decide:
                                  correct → validate → abstain → admit
```

The engine never *guesses*. It admits a fact only if the governed vocabulary (the **packs**)
validates it; otherwise it **abstains** (records "unknown"). Every admitted fact keeps a
**receipt** — the exact source sentence and document.

## The five-layer governed spine

1. **Ingestion** — documents are content-addressed, split into pages/paragraphs (OCR for scans).
2. **Extraction** — a local LLM proposes entity *mentions* and *statements* from prose. The
   model **proposes**; it never decides.
3. **Collapse (GDU — Governed Data Unit)** — the deterministic core. It resolves mentions to
   entities and types each relationship using the packs, admitting only what validates.
4. **Assessment** — the output: typed, sourced edges (`met_with`, `employed`, `owns`, …),
   each with a tier (fact / observation / claim) and its receipts.
5. **Federation (optional, not in the open core)** — nodes reconcile by *content*, not by
   local handles.

## The base-60 substrate

Every unit gets a base-60 address: `[category].[subtype].[instance]` (a Person is `0.0.47`).

- **`category.subtype`** is set by the vocab packs (`entityvocab`/`edgevocab`/`nodevocab`) —
  **global**: identical on every install running the same `active_set`.
- **`instance`** is a **node-local** sequence — so nodes need no global registry and run
  offline / air-gapped.

Cross-node identity is resolved by **content** (canonical name + identifiers like EIN/QID/CIK
via `IDENTIFIER_PRECEDENCE`), never by the local address. The address is a handle; the content
is the identity.

## Governance — why it doesn't fragment

- **Packs** (`libraries/*`) are the versioned vocabulary of how language collapses into
  structure: entity types, relationship edges, verb→edge mappings, collapse policies.
- **`active_set` + content hashes** — `active_set_v6.json` pins each pack version with a
  `sha256`. The spine runs in **strict mode** and **fails closed** if any pack's hash drifts.
  No install can silently fork the rules.
- **The Collapse Commons** — changing a pack (a new edge, a base-60 rule) is a **governed
  version bump**: the content hash changes → the active_set updates → every node re-verifies
  on next boot. The substrate evolves as *one thing*. See [CONTRIBUTING.md](CONTRIBUTING.md).

## The plugin boundary

The open core defines interfaces for advanced features (research orchestration, federation
mesh, media) as **no-op stubs** under `core/research`, `core/federation`, `core/media`
(installed via `core/_stubhook.py`). The core imports and runs stand-alone; the private Enki
platform provides the real implementations. This is the seam between the open engine and the
commercial platform.

## Layout

```
core/            the engine: gdu (collapse), spine, ldu, substrate, documents, extraction, …
libraries/       the 42 governance packs (the vocabulary spine)
services/api/    FastAPI service (routes + the governed pipeline)
ui/              SvelteKit UI (entity/document/library/timeline/map/graph)
storage/postgres/ schema + migrations
active_set_v6.json  the pinned, hash-verified pack set
```
