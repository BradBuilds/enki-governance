# Contributing to Enki Governance — help us collapse language

The **packs** (`libraries/`) are a governed **commons**: a shared, versioned map of how
language collapses into structure — entity types, relationship edges, verb→edge mappings,
and collapse policies. Advancing that map is the heart of the project. If you see a
relationship the engine doesn't know, teach it.

## What you can contribute
- **Verb mappings** *(the common case)* — verbs/phrasings that should collapse into an
  **existing** edge. Reach for this first: an edge is a *concept*, and most new verbs are just
  another surface form of a concept the packs already have.
- **Edges** — a genuinely new relationship *concept* no existing edge covers. Fold **every**
  near-synonym verb into the one edge; don't mint an edge per verb (see the rule below).
- **Entity types** — kinds of things not yet modeled.
- **Glossary terms** — domain vocabulary (legal, finance, intel, …).
- **Corrections** — a mapping that's wrong, or should *abstain* instead of collapsing.

> **The one rule that keeps the commons clean — collapse, don't proliferate.** An edge is a
> base-60 **concept identifier**; a verb is a **form** that collapses into it (`own`, `sells`,
> `acquire`, `purchase` → one `owns` edge). Before proposing an edge, ask *"which existing
> concept does this collapse into?"* Mint a new edge **only** when the answer is *none*, and
> then map all its synonyms to it. One edge per verb — "edges of edges" — fragments a concept
> and defeats collapse. If two edges would fire on the same sentence, they're one concept.

## Contribution is itself governed collapse
Every proposal runs the same gate the engine uses — nothing is merged on vibes:
1. **Correct** — normalize and dedup against existing packs.
2. **Validate** — schema-check, verify the type-pair is legal, check for conflicts, and
   require **evidence** (a real example sentence).
3. **Abstain** — ambiguous or contested proposals go to human review; they are *not*
   auto-merged.
4. **Admit** — accepted proposals become a new pack version and are credited to you
   (`proposed_by`).

## How to propose (builders)
Open a PR adding to the relevant `libraries/*/definitions/*.json`. Example edge:
```json
{
  "edge": "advised",
  "domain": "org",
  "maps_verbs": ["advised", "counseled", "guided"],
  "type_pair": ["person", "org"],
  "examples": ["“Smith advised the board on strategy.”"],
  "proposed_by": "your-handle"
}
```
CI runs **pack-lint** (schema + conflict + dedup). If it fails, the message tells you why.

## How to propose (analysts)
When the engine hits a verb or edge it can't map, it **abstains** and surfaces a
one-click *"propose this edge"* prefilled with the source sentence. That receipt-backed
proposal lands in the review queue. The engine's honesty is the invitation.

## Bar for acceptance
- **Collapse first.** A new *edge* is rejected if the verb collapses into an existing concept —
  it should be a verb mapping instead. New edges must name a concept nothing else covers.
- Real evidence (an example sentence, ideally sourced).
- No conflict with an existing edge/mapping (or an explicit, justified change).
- Passes `pack-lint`.
- Prefer **abstain** over a guess: if a mapping is only sometimes right, say so.

## Ground rules
Be precise, cite examples, and keep it neutral. This is a map of *language*, not of people —
edges describe how words relate, and the data that flows through them always keeps its
receipts.
