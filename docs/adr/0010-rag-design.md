# ADR 0010 — Retrieval for evidence-backed briefs

**Status:** accepted · 2026-09-25

- **Corpus:**
  - Project-authored demo protocols (`knowledge/protocols`, CC BY 4.0, explicitly *not clinical guidance*).
  - A deterministic discharge or ICU summary per patient, rendered from the structured record (no LLM), mirrored
    into FHIR as `DocumentReference`.
  - Structured patient data comes from FHIR and Postgres at query time rather than from embeddings.
- **Index:** `rag_chunks` in the same Postgres. Each `##` section is one chunk with a stable citable id
  (`P-SEPSIS#3`, `DS-p-90000006#1`).
- **Retrieval:** Postgres full-text search (OR-query) and pgvector cosine search, fused with reciprocal rank fusion.
  Each event type adds clinical vocabulary to the query, and the top protocol's *Actions* section is always
  included.
- **Embeddings:** Titan Text Embeddings V2 (1024-d) on AWS, or an offline 1024-d hashing embedder when no
  credentials are available. The embedding model name is stored per chunk, and search filters on it.
- **Grounding:**
  - Briefs must cite chunk ids or live-data block names.
  - `check_citations` rejects unknown references, and a recommendation that cites no protocol is flagged.
  - Clinicians can open the exact source text through `/api/knowledge/{chunk_id}` (reads of patient documents are
    audited).
