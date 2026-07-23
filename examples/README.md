# Example — collapse a document into a governed fact graph

A minimal, end-to-end worked example: one memo in → governed, sourced entities and
relationships out, using the exact same pipeline the engine runs on any document.

## Run it

```bash
docker compose up -d          # if not already running
./scripts/first-run.sh        # pulls the local model, configures the provider
./examples/run.sh
```

## Input — [`sample-memo.txt`](sample-memo.txt)

> On March 5, 2019, **Jane Doe** met with **John Smith**, the chief executive of **Acme
> Corporation**, at the company's **London** office. Smith subsequently hired Doe as a senior
> advisor to the board. Acme Corporation owns **Beta Holdings**, a shipping company
> headquartered in **Rotterdam**. Prior to joining Acme, Doe worked for the **Ministry of
> Transport**.

## What happens

`ingest → (OCR for scans) → split into paragraphs → classify → local LLM extracts mentions +
statements → GOVERNED COLLAPSE → sourced fact graph.`

## Expected output

**Entities** (governed, base-60 addressed) — extracted on CPU or GPU:

```
Jane Doe, John Smith, Acme Corporation, London, Beta Holdings, Rotterdam, Ministry of Transport
```

**Typed, sourced facts** — the full relationship graph (needs a capable model; see the
[LLM/GPU notes](../README.md#the-llm-extraction--cpu-vs-gpu-important)):

```
Jane Doe        --met_with-->        John Smith
John Smith      --employed-->        Jane Doe
Acme Corporation --owns-->           Beta Holdings
Jane Doe        --employed_by-->     Ministry of Transport
```

> On CPU with the small default model you'll reliably get the **entities**; the typed
> **relationships** need a larger model (a GPU). The method is identical either way — only
> the model's proposal quality changes. Every fact keeps a receipt: the exact source sentence
> and document.
