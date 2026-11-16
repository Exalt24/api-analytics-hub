"""Job embeddings: OpenAI text-embedding-3-small through LangChain, stored in Qdrant, searched per tenant.

`make_embeddings` returns LangChain's `OpenAIEmbeddings`. `base_url` points it at OpenAI, at any OpenAI-compatible server (OpenRouter, a
local one) or at the stand-in the tests use. `check_embedding_ctx_length=False` makes it send the text as it is: with the default it sends
token ids, which only OpenAI's own server accepts.

ONE COLLECTION FOR ALL TENANTS, A TENANT PAYLOAD INDEX, AND A FILTER ON EVERY CALL. Qdrant has no row level security. The isolation here
is the filter in `_tenant_filter`, written once and used by every read, and the collection is created with `is_tenant=True` on the tenant
field so Qdrant lays the data out per tenant. A test proves tenant A cannot get tenant B's job back from a nearest neighbour search.

The point id is a UUID derived from the job id, so embedding the same job twice overwrites one point and never makes two.
"""
from __future__ import annotations

import uuid
from typing import Any, Iterable

from langchain_openai import OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore
from qdrant_client import QdrantClient, models

DEFAULT_MODEL = "text-embedding-3-small"
_NS = uuid.UUID("6d1a9c52-6a77-4d3e-9a56-1c0f7f6a1e11")


def make_embeddings(*, api_key: str, base_url: str | None = None, model: str = DEFAULT_MODEL, dimensions: int | None = None) -> OpenAIEmbeddings:
    kwargs: dict[str, Any] = {"model": model, "api_key": api_key, "check_embedding_ctx_length": False, "max_retries": 2, "timeout": 30}
    if base_url:
        kwargs["base_url"] = base_url
    if dimensions:
        kwargs["dimensions"] = dimensions
    return OpenAIEmbeddings(**kwargs)


def point_id(job_id: str) -> str:
    return str(uuid.uuid5(_NS, job_id))


def job_text(job: dict[str, Any]) -> str:
    """What gets embedded: the parts a person searches by, in a fixed order, with the description cut so one long post cannot dominate."""
    parts = [job.get("title"), job.get("company_name"), job.get("location"), "remote" if job.get("remote") else None,
             ", ".join(job.get("technologies") or []), (job.get("description") or "")[:1500]]
    return "\n".join(p for p in parts if p)


class JobIndex:
    def __init__(self, client: QdrantClient, embeddings: OpenAIEmbeddings, *, collection: str = "job_postings"):
        self._client = client
        self._emb = embeddings
        self._collection = collection
        self._store: QdrantVectorStore | None = None

    def _ensure(self) -> QdrantVectorStore:
        if self._store is not None:
            return self._store
        if not self._client.collection_exists(self._collection):
            dim = len(self._emb.embed_query("dimension probe"))
            self._client.create_collection(self._collection, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
            self._client.create_payload_index(
                self._collection, "metadata.tenant_id",
                field_schema=models.KeywordIndexParams(type=models.KeywordIndexType.KEYWORD, is_tenant=True))
        self._store = QdrantVectorStore(client=self._client, collection_name=self._collection, embedding=self._emb)
        return self._store

    def add(self, tenant_id: str, rows: Iterable[dict[str, Any]]) -> int:
        """`rows` are dicts with the posting's `id` and its text fields. Returns how many points were written."""
        rows = list(rows)
        if not rows:
            return 0
        store = self._ensure()
        store.add_texts(
            [job_text(r) for r in rows],
            metadatas=[{"tenant_id": tenant_id, "job_id": str(r["id"]), "title": r.get("title"), "company": r.get("company_name"), "ats": r.get("ats")} for r in rows],
            ids=[point_id(str(r["id"])) for r in rows])
        return len(rows)

    @staticmethod
    def _tenant_filter(tenant_id: str) -> models.Filter:
        if not tenant_id:
            raise ValueError("tenant_id is required")
        return models.Filter(must=[models.FieldCondition(key="metadata.tenant_id", match=models.MatchValue(value=tenant_id))])

    def search(self, tenant_id: str, query: str, *, k: int = 5) -> list[tuple[dict[str, Any], float]]:
        store = self._ensure()
        hits = store.similarity_search_with_score(query, k=k, filter=self._tenant_filter(tenant_id))
        return [(dict(doc.metadata), float(score)) for doc, score in hits]

    def count(self, tenant_id: str) -> int:
        self._ensure()
        return int(self._client.count(self._collection, count_filter=self._tenant_filter(tenant_id), exact=True).count)
