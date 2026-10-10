"""Hybrid retrieval over pgvector + Postgres full-text search, fused with reciprocal rank fusion (RRF)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .documents import Document
from .embeddings import Embedder

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{1,}")


def _vec(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _tsquery(text: str) -> str:
    words = sorted({w.lower() for w in _WORD.findall(text)})
    return " | ".join(words) or "none"


@dataclass
class Hit:
    chunk_id: str
    doc_id: str
    kind: str
    title: str
    heading: str
    text: str
    score: float

    def cite(self) -> str:
        return f"[{self.chunk_id}]"


_SEARCH = """
WITH fts AS (
    SELECT chunk_id, row_number() OVER (ORDER BY ts_rank_cd(tsv, q) DESC) AS r
    FROM rag_chunks, to_tsquery('english', %(tsq)s) q
    WHERE tsv @@ q AND (patient_id IS NULL OR patient_id = %(pid)s) AND (%(kind)s::text IS NULL OR
          chunk_id IN (SELECT chunk_id FROM rag_chunks c JOIN rag_documents d USING (doc_id) WHERE d.kind = %(kind)s))
    ORDER BY ts_rank_cd(tsv, q) DESC LIMIT 40),
vec AS (
    SELECT chunk_id, row_number() OVER (ORDER BY embedding <=> %(emb)s::vector) AS r
    FROM rag_chunks
    WHERE embedding IS NOT NULL AND embedding_model = %(model)s AND (patient_id IS NULL OR patient_id = %(pid)s)
    ORDER BY embedding <=> %(emb)s::vector LIMIT 40)
SELECT c.chunk_id, c.doc_id, d.kind, d.title, c.heading, c.text,
       COALESCE(1.0 / (60 + fts.r), 0) + COALESCE(1.0 / (60 + vec.r), 0) AS score
FROM rag_chunks c JOIN rag_documents d USING (doc_id)
LEFT JOIN fts USING (chunk_id) LEFT JOIN vec USING (chunk_id)
WHERE (fts.chunk_id IS NOT NULL OR vec.chunk_id IS NOT NULL) AND (%(kind)s::text IS NULL OR d.kind = %(kind)s)
ORDER BY score DESC LIMIT %(k)s
"""


class RagIndex:
    def __init__(self, pool: ConnectionPool, embedder: Embedder):
        self.pool, self.embedder = pool, embedder

    def upsert(self, docs: list[Document], batch: int = 32) -> int:
        n = 0
        for doc in docs:
            texts = [f"{doc.title}. {c.heading}. {c.text}" for c in doc.chunks]
            vectors = []
            for i in range(0, len(texts), batch):
                vectors += self.embedder.embed(texts[i:i + batch])
            with self.pool.connection() as conn, conn.transaction():
                conn.execute(
                    "INSERT INTO rag_documents (doc_id, kind, title, source, license, patient_id, content) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (doc_id) DO UPDATE SET kind=EXCLUDED.kind, "
                    "title=EXCLUDED.title, source=EXCLUDED.source, license=EXCLUDED.license, "
                    "patient_id=EXCLUDED.patient_id, content=EXCLUDED.content, updated_at=now()",
                    (doc.doc_id, doc.kind, doc.title, doc.source, doc.license, doc.patient_id, doc.content))
                conn.execute("DELETE FROM rag_chunks WHERE doc_id=%s", (doc.doc_id,))
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO rag_chunks (chunk_id, doc_id, ord, heading, text, patient_id, embedding, "
                        "embedding_model) VALUES (%s,%s,%s,%s,%s,%s,%s::vector,%s)",
                        [(c.chunk_id, doc.doc_id, c.ord, c.heading, c.text, doc.patient_id, _vec(v),
                          self.embedder.name) for c, v in zip(doc.chunks, vectors, strict=True)])
            n += len(doc.chunks)
        return n

    def search(self, query: str, patient_id: str | None = None, k: int = 6, kind: str | None = None) -> list[Hit]:
        emb = self.embedder.embed([query])[0]
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(_SEARCH, {"tsq": _tsquery(query), "pid": patient_id or "", "emb": _vec(emb),
                                         "model": self.embedder.name, "k": k, "kind": kind}).fetchall()
        return [Hit(**r) for r in rows]

    def doc_chunks(self, doc_id: str) -> list[Hit]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT c.chunk_id, c.doc_id, d.kind, d.title, c.heading, c.text, 0.0 AS score FROM rag_chunks c "
                "JOIN rag_documents d USING (doc_id) WHERE doc_id=%s ORDER BY ord", (doc_id,)).fetchall()
        return [Hit(**r) for r in rows]

    def get(self, chunk_ids: list[str]) -> list[Hit]:
        with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            rows = cur.execute(
                "SELECT c.chunk_id, c.doc_id, d.kind, d.title, c.heading, c.text, 0.0 AS score FROM rag_chunks c "
                "JOIN rag_documents d USING (doc_id) WHERE chunk_id = ANY(%s)", (chunk_ids,)).fetchall()
        return [Hit(**r) for r in rows]
