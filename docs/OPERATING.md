<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Operating Enki — growing the vocabulary to fit your corpus

Enki collapses documents using **governed packs** (the vocabulary of entity types and
relationship edges). Out of the box the packs are tuned for people/organizations. When you
point Enki at a **new domain** — CIA declassified files, clinical trials, financial filings,
scientific literature — the engine will correctly **abstain** on relationships it has no edge
for. That's not a failure; it's the signal for how to grow.

This is the operator loop that makes Enki fit *your* documents.

## The loop

```
documents ─► extract ─► collapse ─► [no matching edge?] ─► queue the verb (with evidence)
     ▲                                                                    │
     └──────────── re-collapse ◄── load ◄── author one edge ◄────── review the queue
```

1. **The corpus proposes.** Every document is extracted. A relationship verb that fits no
   edge is written to the **`predicate_review_queue`** — with its frequency and the model's
   reasoning for why it didn't map. The documents are literally telling you what edges you're
   missing, ranked by how often they occur.
2. **You collapse — into the fewest concepts, not one edge per verb.** This is the step
   that goes wrong most often, so it has its own discipline below. An edge is a **concept**
   (a base-60 identifier); a verb is a **surface form** that *collapses into* a concept. Many
   verbs map to one edge — `own`, `sells`, `acquire`, `purchase`, `owner` all collapse into
   the single `owns` concept. So before you add anything, ask:

   > **"Which existing concept does this verb collapse into?"** — *not* "what edge should
   > this verb get?"

   Mint a **new** edge only when the corpus needs a genuinely new *relationship concept* that
   no existing edge covers (`studies`, `funds`, `administered_to` were real gaps). When you do,
   fold **every** near-synonym verb into that one edge — `study, investigate, examine, probe`
   are all forms of one `studies` concept, not four edges. Adding an edge per verb is the
   **"edges of edges"** anti-pattern: it fragments one concept across many identifiers and
   defeats collapse. If two proposed edges would fire on the same sentence, they are probably
   one concept — merge them.

   Mechanically: add the edge to
   `libraries/edgevocab/definitions/edgevocab.edge_types.json`, map **all** its verb forms in
   `edgevocab.predicate_seeds.json`, bump the pack version, and update the content hash in
   `active_set_v6.json`. Evidence-first, never a guess.
3. **Load + re-collapse.** Run the edge/predicate loaders; the queued verbs stop abstaining
   and become sourced facts.
4. **More surface.** Better collapse reveals the next gaps. Repeat — slowly, deliberately.

The discipline matters: the system **abstains and queues** rather than inventing edges, so the
vocabulary only ever grows on evidence. Every edge is a versioned, hash-verified pack change
with an audit trail — see [CONTRIBUTING.md](../CONTRIBUTING.md) for the governance gate.

## AI-assisted operation (VS Code + an AI coding agent)

This loop is **tedious by hand but ideal for an AI coding assistant** — and pairing them is
where Enki gets powerful. Open the repo in **VS Code** with an agent like
**[Claude Code](https://claude.com/claude-code)** and the agent can do the mechanical work
while *you stay the governance authority*:

- **Mine the queue** — "show me the top 20 unmapped verbs with their evidence sentences."
- **Draft grounded proposals** — the agent reads the actual document sentences behind each
  verb and, for each one, **first asks which existing edge concept it collapses into**. It
  proposes a *verb mapping onto an existing edge* by default, and a **new** edge only when no
  concept fits — folding all near-synonyms into that single edge. It cites the evidence and
  never mints an edge per verb (the "edges of edges" anti-pattern — see the loop above).
- **Run the pack ceremony** — bump the version, recompute the content hash, update the active
  set, run the loaders, and re-collapse a sample to confirm facts form.
- **You approve.** The human reviews each proposed edge and says yes/no. The agent never
  decides what's true — it drafts and executes; you govern.

The result is a fast **human-in-the-loop** cycle: the corpus surfaces what's missing, the
agent turns evidence into a clean pack change, you approve, and the graph gets richer — a
self-improving governance loop where every step is auditable and every claim keeps its receipt.

> This works with any capable coding agent; Claude Code in VS Code is simply the setup we use.
> The governance guarantees (abstention, versioned packs, receipts) hold no matter who — human
> or agent — drives the loop.
