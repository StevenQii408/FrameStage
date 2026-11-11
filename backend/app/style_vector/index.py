"""Versioned SQLite/NumPy index and dimension-isolated retrieval."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import numpy as np

from app.db import Database, utc_now
from app.style_vector.embedding import DIMENSION, TEXT_MODEL, VISION_MODEL, normalized
from app.style_vector.ontology import CATEGORIES, RECIPES, VERSION, ontology_hash, terms, validate_catalog


class IndexNotReady(RuntimeError):
    pass


def _cache_key(model: str, source: bytes) -> str:
    return sha256((model + f"|{DIMENSION}|{ontology_hash()}|").encode() + source).hexdigest()


class StyleVectorIndex:
    def __init__(self, db: Database, embedder):
        self.db = db
        self.embedder = embedder
        self.db.init()
        self._matrices: dict[tuple[str, str], tuple[list[str], np.ndarray]] = {}

    def manifest(self) -> dict | None:
        row = self.db.fetchone("SELECT payload_json FROM style_index_manifest WHERE id=1")
        return json.loads(row["payload_json"]) if row else None

    def health(self) -> dict:
        manifest = self.manifest()
        ready = bool(manifest and manifest.get("ontology_hash") == ontology_hash()
                     and manifest.get("dimension") == DIMENSION
                     and manifest.get("text_model") == TEXT_MODEL
                     and manifest.get("vision_model") == VISION_MODEL
                     and manifest.get("recipe_hash") == recipe_hash())
        return {"ready": ready, "reason": "" if ready else "向量索引缺失或版本不匹配，请运行 python -m app.style_index build",
                "manifest": manifest}

    def _cached(self, model: str, source: bytes):
        key = _cache_key(model, source)
        row = self.db.fetchone("SELECT vector FROM style_embedding_cache WHERE cache_key=? AND model=? AND dimension=? AND ontology_hash=?",
                               (key, model, DIMENSION, ontology_hash()))
        return normalized(np.frombuffer(row["vector"], dtype=np.float32)) if row else None

    def _put_cache(self, model: str, source: bytes, vector: np.ndarray):
        self.db.execute(
            "INSERT OR REPLACE INTO style_embedding_cache(cache_key,model,dimension,ontology_hash,vector,created_at) VALUES(?,?,?,?,?,?)",
            (_cache_key(model, source), model, DIMENSION, ontology_hash(), vector.tobytes(), utc_now()))

    def _text_vectors(self, inputs: list[str], *, visual: bool) -> list[np.ndarray]:
        model = VISION_MODEL if visual else TEXT_MODEL
        result: list[np.ndarray | None] = [self._cached(model, item.encode()) for item in inputs]
        missing = [index for index, vector in enumerate(result) if vector is None]
        batch_size = 20 if visual else 10
        batches = [missing[start:start + batch_size] for start in range(0, len(missing), batch_size)]
        call = self.embedder.vision_text if visual else self.embedder.text
        workers = min(4, max(1, getattr(getattr(self.embedder, "settings", None),
                                        "style_embed_concurrency", 1)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            responses = list(executor.map(lambda indexes: call([inputs[index] for index in indexes]), batches))
        for indexes, vectors in zip(batches, responses):
            if len(vectors) != len(indexes):
                raise ValueError("Embedding 响应数量与请求不一致")
            for index, vector in zip(indexes, vectors):
                result[index] = vector
                self._put_cache(model, inputs[index].encode(), vector)
        if any(item is None for item in result):
            raise ValueError("Embedding 结果不完整")
        return result

    def build(self) -> dict:
        validate_catalog()
        catalogue = terms()
        text_items = [(item.id, index, view) for item in catalogue.values()
                      for index, view in enumerate(item.retrieval_views())]
        text_vectors = self._text_vectors([item[2] for item in text_items], visual=False)
        vision_items = list(catalogue.values())
        vision_vectors = self._text_vectors([item.retrieval_views()[0] for item in vision_items], visual=True)
        recipes = list(RECIPES.values())
        self._text_vectors([item.retrieval_text() for item in recipes], visual=False)
        self._text_vectors([item.retrieval_text() for item in recipes], visual=True)
        if len(text_vectors) != len(text_items) or len(vision_vectors) != len(vision_items):
            raise ValueError("向量索引构建不完整")
        manifest = {"version": "style-vector.v2", "ontology_version": VERSION, "ontology_hash": ontology_hash(),
                    "text_model": TEXT_MODEL, "vision_model": VISION_MODEL, "dimension": DIMENSION,
                    "term_count": len(catalogue), "text_vectors": len(text_vectors),
                    "vision_vectors": len(vision_vectors), "recipe_count": len(recipes),
                    "recipe_hash": recipe_hash(), "built_at": utc_now()}
        # No table is replaced until every remote call and vector check succeeded.
        with self.db.connect() as conn:
            conn.execute("DELETE FROM style_embeddings")
            conn.execute("DELETE FROM style_terms_v2")
            conn.execute("DELETE FROM style_relations_v2")
            for item in catalogue.values():
                conn.execute("INSERT INTO style_terms_v2 VALUES(?,?,?,?)",
                             (item.id, item.category, json.dumps(asdict(item), ensure_ascii=False), ontology_hash()))
                for other in item.conflicts_with:
                    conn.execute("INSERT OR IGNORE INTO style_relations_v2 VALUES(?,?,?)", (item.id, "conflicts_with", other))
                for other in item.compatible_with:
                    conn.execute("INSERT OR IGNORE INTO style_relations_v2 VALUES(?,?,?)", (item.id, "compatible_with", other))
            for (term_id, view_index, _), vector in zip(text_items, text_vectors):
                conn.execute("INSERT INTO style_embeddings VALUES(?,?,?,?,?,?,?)",
                             (term_id, "text", view_index, TEXT_MODEL, DIMENSION, ontology_hash(), vector.tobytes()))
            for item, vector in zip(vision_items, vision_vectors):
                conn.execute("INSERT INTO style_embeddings VALUES(?,?,?,?,?,?,?)",
                             (item.id, "vision", 0, VISION_MODEL, DIMENSION, ontology_hash(), vector.tobytes()))
            conn.execute("INSERT OR REPLACE INTO style_index_manifest(id,payload_json) VALUES(1,?)",
                         (json.dumps(manifest, ensure_ascii=False),))
        self._matrices.clear()
        return manifest

    def _matrix(self, channel: str, category: str) -> tuple[list[str], np.ndarray]:
        if category not in CATEGORIES or channel not in ("text", "vision"):
            raise ValueError("未知画风维度或向量通道")
        key = channel, category
        if key not in self._matrices:
            rows = self.db.fetchall(
                "SELECT e.term_id,e.vector FROM style_embeddings e JOIN style_terms_v2 t ON t.id=e.term_id "
                "WHERE e.channel=? AND t.category=? AND e.ontology_hash=? ORDER BY e.term_id,e.view_index",
                (channel, category, ontology_hash()))
            if not rows:
                raise IndexNotReady(self.health()["reason"])
            ids = [row["term_id"] for row in rows]
            matrix = np.vstack([normalized(np.frombuffer(row["vector"], dtype=np.float32)) for row in rows])
            self._matrices[key] = ids, matrix
        return self._matrices[key]

    def search(self, channel: str, category: str, query: np.ndarray, limit: int = 5) -> list[dict]:
        if not self.health()["ready"]:
            raise IndexNotReady(self.health()["reason"])
        ids, matrix = self._matrix(channel, category)
        scores = np.einsum("ij,j->i", matrix, normalized(query), optimize=False)
        by_id: dict[str, float] = {}
        for term_id, score in zip(ids, scores):
            by_id[term_id] = max(by_id.get(term_id, -1.0), float(score))
        return [{"term_id": term_id, "score": score, "rank": rank}
                for rank, (term_id, score) in enumerate(
                    sorted(by_id.items(), key=lambda pair: (-pair[1], pair[0]))[:limit], 1)]

    def query_text(self, text: str) -> np.ndarray:
        return self._text_vectors([text], visual=False)[0]

    def query_image(self, frame_path: str | Path) -> np.ndarray:
        data = Path(frame_path).read_bytes()
        cached = self._cached(VISION_MODEL, data)
        if cached is not None:
            return cached
        vector, _ = self.embedder.vision_image(frame_path)
        self._put_cache(VISION_MODEL, data, vector)
        return vector

    def search_recipes(self, channel: str, query: np.ndarray, limit: int = 5) -> list[dict]:
        if channel not in ("text", "vision"):
            raise ValueError("未知配方向量通道")
        recipes = list(RECIPES.values())
        # Full recipe descriptions are indexed in their own spaces and cached.
        # Existing term indexes remain usable; the small recipe cache is filled
        # lazily once, or explicitly by `style_index build`.
        matrix = np.vstack(self._text_vectors([item.retrieval_text() for item in recipes],
                                             visual=channel == "vision"))
        # Match the term index's deterministic NumPy path; avoids platform
        # BLAS matmul anomalies on the older macOS Python environment.
        scores = np.einsum("ij,j->i", matrix, normalized(query), optimize=False)
        ordered = sorted(zip(recipes, scores), key=lambda pair: (-float(pair[1]), pair[0].id))[:limit]
        return [{"term_id": recipe.id, "score": float(score), "rank": rank}
                for rank, (recipe, score) in enumerate(ordered, 1)]


def recipe_hash() -> str:
    return sha256(json.dumps([asdict(item) for item in RECIPES.values()],
                             ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def rank_fusion(text_rank: list[dict], image_rank: list[dict], aliases: list[str], limit: int = 8) -> list[dict]:
    """RRF combines ranks, never incomparable cosine values."""
    fused: dict[str, dict] = {}
    for channel, rows in (("text", text_rank), ("image", image_rank)):
        for row in rows:
            item = fused.setdefault(row["term_id"], {"term_id": row["term_id"], "rrf": 0.0, "ranks": {}})
            item["rrf"] += 1 / (60 + row["rank"])
            item["ranks"][channel] = row["rank"]
    for term_id in aliases:
        item = fused.setdefault(term_id, {"term_id": term_id, "rrf": 0.0, "ranks": {}})
        item["rrf"] += 1 / 60
        item["ranks"]["alias"] = 1
    return sorted(fused.values(), key=lambda item: (-item["rrf"], item["term_id"]))[:limit]


def exact_aliases(text: str, category: str) -> list[str]:
    lowered = text.lower()
    return [term.id for term in terms().values() if term.category == category
            and any(len(alias) >= 2 and alias.lower() in lowered for alias in term.aliases)]
