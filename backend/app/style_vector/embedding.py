"""Two cloud embedding spaces. Tokens and vectors never enter frontend payloads."""
from __future__ import annotations

import base64
from hashlib import sha256
from app.db import utc_now
import math
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx
import numpy as np


TEXT_MODEL = "text-embedding-v4"
VISION_MODEL = "qwen3-vl-embedding"
DIMENSION = 1024
TEXT_PRICE_CNY_PER_TOKEN = 0.0005 / 1000
VISION_TEXT_PRICE_CNY_PER_TOKEN = 0.0007 / 1000
VISION_IMAGE_PRICE_CNY_PER_TOKEN = 0.0018 / 1000


def normalized(values, dimension: int = DIMENSION) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32)
    if vector.shape != (dimension,) or not np.isfinite(vector).all():
        raise ValueError("Embedding 维度或数值无效")
    norm = float(np.linalg.norm(vector))
    if norm <= 0 or not math.isfinite(norm):
        raise ValueError("Embedding 零向量无效")
    return np.ascontiguousarray(vector / norm, dtype=np.float32)


def persist_usage(db, records: list[dict], job_id: str | None = None) -> None:
    if not records:
        return
    with db.connect() as conn:
        for row in records:
            conn.execute(
                "INSERT INTO style_embedding_usage(job_id,phase,model,request_id,prompt_tokens,image_tokens,total_tokens,elapsed_seconds,estimated_list_price_cny,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, row.get("phase", ""), row.get("model", ""), row.get("request_id"),
                 row.get("prompt_tokens"), row.get("image_tokens"), row.get("total_tokens"),
                 row.get("elapsed_seconds"), row.get("estimated_list_price_cny"), row.get("status", "unknown"), utc_now()))


class BailianStyleEmbedder:
    def __init__(self, settings, *, client: httpx.Client | None = None):
        self.settings = settings
        if (getattr(settings, "style_text_embed_model", TEXT_MODEL) != TEXT_MODEL or
            getattr(settings, "style_vision_embed_model", VISION_MODEL) != VISION_MODEL or
            getattr(settings, "style_embed_dimension", DIMENSION) != DIMENSION):
            raise ValueError("Style Vector v2 目前固定支持 text-embedding-v4 / qwen3-vl-embedding / 1024维")
        self.client = client or httpx.Client()
        self._owns_client = client is None
        self.usage: list[dict] = []
        base = settings.cloud_base_url.rstrip("/")
        suffix = "/compatible-mode/v1"
        if not base.endswith(suffix):
            raise ValueError("向量模型需使用百炼 compatible-mode/v1 北京工作区地址")
        host = base[:-len(suffix)]
        parsed = urlsplit(host)
        if parsed.scheme != "https" or not parsed.hostname or not parsed.hostname.endswith(".cn-beijing.maas.aliyuncs.com"):
            raise ValueError("向量模型仅使用百炼北京工作区 HTTPS 地址")
        self.text_url = base + "/embeddings"
        self.vision_url = host + "/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"

    def _request(self, url: str, payload: dict, channel: str) -> dict:
        if not self.settings.cloud_api_key:
            raise RuntimeError("未配置百炼 API Key")
        started = time.monotonic()
        record = {"phase": f"style.embedding.{channel}", "model": payload["model"], "status": "failed",
                  "usage_available": False, "estimated_list_price_cny": None}
        self.usage.append(record)
        try:
            response = self.client.post(url, json=payload,
                                        headers={"Authorization": f"Bearer {self.settings.cloud_api_key}"},
                                        timeout=min(45, self.settings.vlm_timeout_seconds))
            response.raise_for_status()
            result = response.json()
            usage = result.get("usage") or {}
            record["status"] = "success"
            record["request_id"] = result.get("request_id") or result.get("id")
            record["prompt_tokens"] = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            record["image_tokens"] = int(usage.get("image_tokens") or 0)
            record["total_tokens"] = int(usage.get("total_tokens") or 0)
            record["usage_available"] = bool(usage)
            if usage:
                record["estimated_list_price_cny"] = round(
                    record["prompt_tokens"] * (TEXT_PRICE_CNY_PER_TOKEN if channel == "text" else VISION_TEXT_PRICE_CNY_PER_TOKEN)
                    + record["image_tokens"] * VISION_IMAGE_PRICE_CNY_PER_TOKEN, 8)
            return result
        except httpx.HTTPStatusError as exc:
            record["error"] = f"HTTP {exc.response.status_code}"
            raise RuntimeError(f"百炼向量请求失败：HTTP {exc.response.status_code}") from exc
        except Exception as exc:
            record["error"] = type(exc).__name__
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)

    def text(self, texts: list[str]) -> list[np.ndarray]:
        if not 1 <= len(texts) <= 10 or any(not item.strip() for item in texts):
            raise ValueError("text-embedding-v4 每批需 1–10 条非空文本")
        result = self._request(self.text_url, {"model": TEXT_MODEL, "input": texts,
                                               "dimensions": DIMENSION, "encoding_format": "float"}, "text")
        rows = sorted(result.get("data") or [], key=lambda item: item["index"])
        if len(rows) != len(texts) or [row["index"] for row in rows] != list(range(len(texts))):
            raise ValueError("文本向量响应数量或顺序不符")
        return [normalized(row["embedding"]) for row in rows]

    def vision_text(self, texts: list[str]) -> list[np.ndarray]:
        if not 1 <= len(texts) <= 20 or any(not item.strip() for item in texts):
            raise ValueError("视觉向量文本批次无效")
        result = self._request(self.vision_url, {
            "model": VISION_MODEL, "input": {"contents": [{"text": item} for item in texts]},
            "parameters": {"dimension": DIMENSION, "enable_fusion": False},
        }, "vision")
        rows = sorted((result.get("output") or {}).get("embeddings") or [], key=lambda item: item["index"])
        if len(rows) != len(texts) or [row["index"] for row in rows] != list(range(len(texts))):
            raise ValueError("视觉文本向量响应数量或顺序不符")
        return [normalized(row["embedding"]) for row in rows]

    def vision_image(self, image_path: str | Path) -> tuple[np.ndarray, str]:
        path = Path(image_path)
        if path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
            raise ValueError("仅支持 JPG/PNG 关键帧")
        data = path.read_bytes()
        if len(data) > 10 * 1024 * 1024:
            raise ValueError("关键帧超过视觉向量模型 10MB 限制")
        digest = sha256(data).hexdigest()
        mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        encoded = base64.b64encode(data).decode("ascii")
        result = self._request(self.vision_url, {
            "model": VISION_MODEL,
            "input": {"contents": [{"image": f"data:{mime};base64,{encoded}"}]},
            "parameters": {"dimension": DIMENSION, "enable_fusion": False},
        }, "vision")
        rows = (result.get("output") or {}).get("embeddings") or []
        if len(rows) != 1 or rows[0].get("index") != 0:
            raise ValueError("视觉图像向量响应数量不符")
        return normalized(rows[0]["embedding"]), digest

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def drain_usage(self) -> list[dict]:
        records, self.usage = self.usage, []
        return records
