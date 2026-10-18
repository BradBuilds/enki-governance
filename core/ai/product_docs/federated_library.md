# What a Library is

A **library** is a topic-scoped corpus of collected documents plus everything the
system extracted and collapsed from them — the entities, facts, events, and a
knowledge graph. Example: the "Alex Acme" library holds hundreds of court
records and, on top of them, thousands of resolved entities and a timeline.

Each library shows **coverage** (how many source references have been collapsed
of the total known), a document/entity/fact count, and tabs for Overview,
Timeline, Entities, Events, Map, and Chat.

# How collapse builds a library

Raw documents go through a governed pipeline (the GDU spine): text is extracted,
entities/places/facts/events are pulled out, and then **collapsed** — many
mentions of the same thing across many documents fuse into one canonical node.
The result is deterministic: the same documents produce the same graph. Facts
carry corroboration counts (how many independent sources confirm them) and
contradiction counts, so you can see how well-supported a claim is.

# The public / federated library

Libraries are not locked to one machine. A node can **publish** a library so
other nodes can subscribe to it, and nodes can **federate** — sharing entities,
memberships, and extractions across a mesh. This is how a "public library"
works: one node curates and collapses a corpus, then federates it so others get
the same collapsed graph without re-doing the work. Federation carries signed,
content-addressed evidence, so a downstream node can verify what it received.

Publishing/federation is handled by the federation layer (publisher, entity
merge, membership, onboarding). A neutral publisher and capture-on-mine model
means originals are preserved and never silently deleted.

# Using a library

- **Start project run** kicks off collecting + collapsing more of the corpus.
- **Timeline / Entities / Events** browse the collapsed graph.
- **Chat** (this assistant), scoped to the library, answers questions about just
  that corpus — "what's new this week", "who are the key people", "list facts
  about X".
- **Map** plots the geolocated entities and events for the library.
