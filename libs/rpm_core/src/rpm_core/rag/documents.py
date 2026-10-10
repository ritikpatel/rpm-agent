"""Documents and chunks. Protocols: markdown with front matter; each `##` section is one citable chunk."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

PROTOCOL_LICENSE = "CC BY 4.0 (project-authored demo protocol; not clinical guidance)"


@dataclass
class Chunk:
    chunk_id: str
    ord: int
    heading: str
    text: str


@dataclass
class Document:
    doc_id: str
    kind: str
    title: str
    source: str
    license: str
    content: str
    patient_id: str | None = None
    chunks: list[Chunk] = field(default_factory=list)


def split_sections(doc_id: str, markdown: str) -> list[Chunk]:
    chunks, heading, buf = [], "Overview", []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            chunks.append(Chunk(f"{doc_id}#{len(chunks) + 1}", len(chunks) + 1, heading, text))

    for line in markdown.splitlines():
        m = re.match(r"^##\s+(.*)", line)
        if m:
            flush()
            heading, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    flush()
    return chunks


def load_protocols(directory: str | Path) -> list[Document]:
    docs = []
    for path in sorted(Path(directory).glob("*.md")):
        raw = path.read_text()
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
        if not m:
            continue
        meta = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
        doc_id, body = meta["id"].strip(), m.group(2)
        docs.append(Document(doc_id, "protocol", meta["title"].strip(), f"knowledge/protocols/{path.name}",
                             PROTOCOL_LICENSE, body, None, split_sections(doc_id, body)))
    return docs
