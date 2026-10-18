"""Worked example — collapse examples/sample-memo.txt into a governed fact graph.
Run it with examples/run.sh (which executes this inside the api container)."""
import time
from storage.postgres.db import get_connection
from core.enki.ingest import ingest_document
from core.workers.collapse_worker import process_document
from core.documents.ingester import persist_pages_with_paragraphs

TEXT = open("/tmp/sample-memo.txt", encoding="utf-8").read()

# get-or-create an example library
with get_connection() as conn, conn.cursor() as cur:
    cur.execute("SELECT library_id FROM libraries WHERE canonical_name='Example' LIMIT 1")
    row = cur.fetchone()
    if row:
        lib = row[0]
    else:
        cur.execute(
            "INSERT INTO libraries (base60_address, library_subtype, canonical_name, "
            "focal_term, description, tenant_id, discovery_status) VALUES "
            "('example-lib', 'corpus_library', 'Example', 'example', 'Worked example', "
            "'default', 'active') RETURNING library_id")
        lib = cur.fetchone()[0]
    conn.commit()

# ingest the document, split into paragraphs, then extract + collapse
res = ingest_document(body=TEXT.encode(), source_url="example://memo",
                      source_title="Example memo", mime_type="text/plain", library_id=lib)
doc_id = res.document_id
persist_pages_with_paragraphs(doc_id, [TEXT], extraction_method="text")
print(f"ingested document {doc_id}; extracting + collapsing with the local LLM…", flush=True)
t = time.time()
process_document(doc_id, lib)
print(f"done in {time.time() - t:.0f}s\n", flush=True)

# show what collapsed
with get_connection() as conn, conn.cursor() as cur:
    cur.execute("SELECT canonical_name FROM entities ORDER BY entity_id")
    entities = [r[0] for r in cur.fetchall()]
    cur.execute("""
        SELECT e1.canonical_name, a.predicate_edge_type,
               COALESCE(e2.canonical_name, a.obj_value)
          FROM assessments a
          JOIN entities e1 ON e1.entity_id = a.subj_entity_id
          LEFT JOIN entities e2 ON e2.entity_id = a.obj_entity_id
         ORDER BY a.assessment_id""")
    facts = cur.fetchall()

print("ENTITIES (%d): %s" % (len(entities), ", ".join(entities)))
print("\nTYPED, SOURCED FACTS (%d):" % len(facts))
for s, p, o in facts:
    print(f"  {s}  --{p}-->  {o}")
if not facts:
    print("  (none yet — on CPU + the small default model, relationship extraction is")
    print("   limited. Set OLLAMA_MODEL to a larger model on a GPU for the full graph.)")
