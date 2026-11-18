"""Qwen and embedded Chroma I/O, outside the pure semantic provider."""

import hashlib
import json
import math
import os
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class EmbeddingSettings:
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    base_url: str = ""
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_env(cls):
        return cls(
            model=os.getenv("QWEN_EMBEDDING_MODEL", "text-embedding-v4"),
            dimensions=int(os.getenv("QWEN_EMBEDDING_DIMENSIONS", "1024")),
            base_url=os.getenv("QWEN_EMBEDDING_BASE_URL", ""),
            api_key=os.getenv("QWEN_EMBEDDING_API_KEY", ""),
        )

    def identity(self):
        return {"model": self.model, "dimensions": self.dimensions, "endpoint": self.base_url.rstrip("/")}


class QwenEmbedder:
    def __init__(self, settings: EmbeddingSettings):
        self.settings = settings

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self.settings.api_key or not self.settings.base_url:
            raise ValueError("Configure QWEN_EMBEDDING_API_KEY and QWEN_EMBEDDING_BASE_URL in .env")
        from openai import OpenAI

        vectors = []
        # One retry owner; 401/403 are not retried by the SDK.
        with OpenAI(
            api_key=self.settings.api_key, base_url=self.settings.base_url, timeout=30, max_retries=1
        ) as client:
            for start in range(0, len(texts), 10):
                response = client.embeddings.create(
                    model=self.settings.model,
                    input=texts[start : start + 10],
                    dimensions=self.settings.dimensions,
                    encoding_format="float",
                )
                batch = sorted(response.data, key=lambda item: item.index)
                if len(batch) != len(texts[start : start + 10]):
                    raise ValueError("Embedding service returned an incomplete batch")
                vectors.extend(item.embedding for item in batch)
        if any(len(v) != self.settings.dimensions or not all(math.isfinite(n) for n in v) for v in vectors):
            raise ValueError("Embedding service returned invalid vector dimensions or values")
        return vectors


def catalog_documents(catalog, semantic, overlay: dict | None = None):
    """Only metadata is indexed; canonical source records and business rows stay untouched."""
    overlay = overlay or {}
    docs: dict[str, list[dict]] = {"tables": [], "fields": [], "metrics": []}
    metrics = semantic.list_metrics()
    rules = semantic.list_rules()
    for table in sorted(catalog.get_table_list()):
        info = catalog.get_table_information(table) if hasattr(catalog, "get_table_information") else {}
        columns = catalog.get_column_list(table)
        related = [metric for metric in metrics if table in metric.source_tables]
        table_text = " ".join(
            [
                table,
                str(info.get("description", "")),
                str(overlay.get("tables", {}).get(table, "")),
                *(f"{m.label} {m.definition}" for m in related),
                *(col["column_name"] for col in columns),
            ]
        )
        docs["tables"].append({"id": table, "text": table_text, "metadata": {"table": table}})
        for col in columns:
            name = col["column_name"]
            qualified = f"{table}.{name}"
            notes = [r.note for r in rules if qualified in (*r.require_columns, *r.forbid_columns)]
            text = " ".join(
                [
                    qualified,
                    str(col.get("display_name", "")),
                    str(col.get("description", "")),
                    str(col.get("alias", "")),
                    str(col.get("type", "")),
                    str(overlay.get("fields", {}).get(qualified, "")),
                    *notes,
                ]
            )
            docs["fields"].append({"id": qualified, "text": text, "metadata": {"table": table, "field": name}})
    for metric in metrics:
        text = " ".join([metric.name, metric.label, *metric.aliases, metric.definition, metric.formula or ""])
        docs["metrics"].append({"id": metric.name, "text": text, "metadata": {"metric": metric.name}})
    return docs


class RetrievalService:
    def __init__(self, catalog, semantic, directory: str, *, settings=None, embedder=None, overlay=None):
        self.semantic = semantic
        self.documents = catalog_documents(catalog, semantic, overlay)
        self.directory = Path(directory)
        self.settings = settings or EmbeddingSettings.from_env()
        self.embedder = embedder or QwenEmbedder(self.settings)
        self.snapshot_id = digest({"documents": self.documents, "semantic": semantic.snapshot_id})
        self.fingerprint = digest({"snapshot": self.snapshot_id, "embedding": self.settings.identity()})
        self._client = None
        self._query_cache: dict[str, list[float]] = {}
        self._lock = threading.RLock()

    @property
    def manifest_path(self):
        return self.directory / "manifest.json"

    def _db(self):
        with self._lock:
            if self._client is None:
                import chromadb
                from chromadb.config import Settings

                self._client = chromadb.PersistentClient(
                    path=str(self.directory), settings=Settings(anonymized_telemetry=False)
                )
            return self._client

    def manifest(self):
        if not self.manifest_path.exists():
            raise ValueError("Semantic vector index is missing; run python -m scripts.build_semantic_index")
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if value.get("fingerprint") != self.fingerprint:
            raise ValueError("Semantic vector index is stale; rebuild it for the current metadata and embedding model")
        return value

    def build(self):
        """Publish one complete generation atomically; never reset the database."""
        from filelock import FileLock

        self.directory.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.directory / "build.lock")), self._lock:
            previous = None
            if self.manifest_path.exists():
                previous = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if previous.get("fingerprint") == self.fingerprint:
                    return previous
            db = self._db()
            generation = uuid.uuid4().hex[:16]
            collections = {}
            same_model = previous and previous.get("embedding") == self.settings.identity()
            cached_vectors = {}
            if same_model and previous.get("vectors_file"):
                cache_file = self.directory / f"vectors.{previous['generation']}.json"
                if cache_file.exists():
                    cached_vectors = json.loads(cache_file.read_text(encoding="utf-8"))
            next_vectors = {}
            for kind, documents in self.documents.items():
                collection = db.create_collection(
                    f"{kind}_{generation}",
                    embedding_function=None,
                    configuration={"hnsw": {"space": "cosine", "batch_size": 1, "sync_threshold": 1}},
                )
                reusable = {}
                if kind in cached_vectors:
                    reusable = {key: (row["hash"], row["vector"]) for key, row in cached_vectors[kind].items()}
                elif same_model:
                    old = db.get_collection(previous["collections"][kind], embedding_function=None).get(
                        include=["embeddings", "metadatas"]
                    )
                    reusable = {
                        key: (metadata.get("document_hash"), vector)
                        for key, metadata, vector in zip(old["ids"], old["metadatas"], old["embeddings"], strict=True)
                    }
                missing = [
                    doc for doc in documents if doc["id"] not in reusable or reusable[doc["id"]][0] != digest(doc)
                ]
                fresh = dict(
                    zip((doc["id"] for doc in missing), self.embedder.embed([d["text"] for d in missing]), strict=True)
                )
                for start in range(0, len(documents), 100):
                    batch = documents[start : start + 100]
                    collection.upsert(
                        ids=[d["id"] for d in batch],
                        documents=[d["text"] for d in batch],
                        metadatas=[{**d["metadata"], "document_hash": digest(d)} for d in batch],
                        embeddings=[fresh[d["id"]] if d["id"] in fresh else reusable[d["id"]][1] for d in batch],
                    )
                next_vectors[kind] = {
                    doc["id"]: {
                        "hash": digest(doc),
                        "vector": [
                            float(value)
                            for value in (fresh[doc["id"]] if doc["id"] in fresh else reusable[doc["id"]][1])
                        ],
                    }
                    for doc in documents
                }
                collections[kind] = collection.name
            manifest = {
                "fingerprint": self.fingerprint,
                "snapshot_id": self.snapshot_id,
                "generation": generation,
                "collections": collections,
                "embedding": self.settings.identity(),
                "counts": {kind: len(docs) for kind, docs in self.documents.items()},
                "vectors_file": f"vectors.{generation}.json",
            }
            vector_file = self.directory / manifest["vectors_file"]
            vector_file.write_text(json.dumps(next_vectors), encoding="utf-8")
            temporary = self.directory / f"manifest.{generation}.tmp"
            temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.manifest_path)
            return manifest

    def search(self, kind: str, query: str, top_k: int, tables: list[str] | None = None):
        if not query.strip():
            raise ValueError("A nonempty retrieval query is required")
        manifest = self.manifest()
        collection = self._db().get_collection(manifest["collections"][kind], embedding_function=None)
        if not collection.count():
            return []
        with self._lock:
            if query not in self._query_cache:
                if len(self._query_cache) >= 128:
                    self._query_cache.pop(next(iter(self._query_cache)))
                self._query_cache[query] = self.embedder.embed([query])[0]
            vector = self._query_cache[query]
        kwargs = {"where": {"table": {"$in": tables}}} if tables else {}
        result = collection.query(
            query_embeddings=[vector],
            n_results=min(top_k, collection.count()),
            include=["distances", "metadatas"],
            **kwargs,
        )
        return [
            {"id": key, "distance": float(distance), **metadata}
            for key, distance, metadata in zip(
                result["ids"][0], result["distances"][0], result["metadatas"][0], strict=True
            )
        ]
