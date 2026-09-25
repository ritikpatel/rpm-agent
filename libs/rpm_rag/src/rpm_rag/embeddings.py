"""Embedding providers. `titan` = Amazon Titan Text Embeddings V2 on Bedrock (production). `hash` = offline
feature-hashing embedder (no credentials; lexical similarity only) so retrieval works in dev and CI."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Protocol

DIM = 1024
_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    name = "hash-1024"

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vec = [0.0] * DIM
            toks = _TOKEN.findall(text.lower())
            grams = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
            for g in grams:
                h = int.from_bytes(hashlib.blake2b(g.encode(), digest_size=8).digest(), "big")
                vec[h % DIM] += 1.0 if (h >> 63) & 1 else -1.0
            vec = [math.copysign(math.log1p(abs(x)), x) for x in vec]
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class TitanEmbedder:
    name = "amazon.titan-embed-text-v2:0"

    def __init__(self, region: str):
        import boto3

        self.client = boto3.client("bedrock-runtime", region_name=region)

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            resp = self.client.invoke_model(modelId=self.name, contentType="application/json",
                                            body=json.dumps({"inputText": text[:30000], "dimensions": DIM,
                                                             "normalize": True}))
            out.append(json.loads(resp["body"].read())["embedding"])
        return out


def make_embedder(provider: str, region: str = "us-east-1") -> Embedder:
    return TitanEmbedder(region) if provider == "titan" else HashEmbedder()
