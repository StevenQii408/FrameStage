from types import SimpleNamespace

import httpx
import pytest

from app.style_vector.embedding import BailianStyleEmbedder


def settings(host="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"):
    return SimpleNamespace(
        cloud_base_url=host, cloud_api_key="test-only-key", vlm_timeout_seconds=180,
        style_text_embed_model="text-embedding-v4", style_vision_embed_model="qwen3-vl-embedding",
        style_embed_dimension=1024,
    )


def test_bailian_text_and_native_multimodal_paths(tmp_path):
    calls = []
    vector = [1.0] + [0.0] * 1023
    def handler(request: httpx.Request):
        calls.append((request.url.path, request.content.decode()))
        assert request.headers["authorization"] == "Bearer test-only-key"
        payload = __import__("json").loads(request.content)
        if request.url.path.endswith("/compatible-mode/v1/embeddings"):
            assert payload["model"] == "text-embedding-v4"
            assert payload["dimensions"] == 1024
            return httpx.Response(200, json={
                "data": [{"index": i, "embedding": vector} for i in range(len(payload["input"]))],
                "usage": {"prompt_tokens": 20, "total_tokens": 20},
            })
        assert request.url.path.endswith("/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding")
        assert payload["model"] == "qwen3-vl-embedding"
        assert payload["parameters"] == {"dimension": 1024, "enable_fusion": False}
        content = payload["input"]["contents"]
        assert all("text" in item or item["image"].startswith("data:image/jpeg;base64,") for item in content)
        return httpx.Response(200, json={
            "output": {"embeddings": [{"index": i, "embedding": vector} for i in range(len(content))]},
            "usage": {"input_tokens": 30, "image_tokens": 10, "total_tokens": 40},
        })
    client = httpx.Client(transport=httpx.MockTransport(handler))
    embedder = BailianStyleEmbedder(settings(), client=client)
    assert len(embedder.text(["蓝灰", "低饱和"])) == 2
    assert len(embedder.vision_text(["剪纸", "纸纹"])) == 2
    path = tmp_path / "frame.jpg"
    path.write_bytes(b"image-fixture")
    image, digest = embedder.vision_image(path)
    assert image.shape == (1024,) and len(digest) == 64
    usage = embedder.drain_usage()
    assert len(calls) == len(usage) == 3
    assert all(row["estimated_list_price_cny"] is not None for row in usage)
    assert embedder.drain_usage() == []


def test_embedding_rejects_proxy_and_mismatched_model_config():
    with pytest.raises(ValueError, match="北京工作区"):
        BailianStyleEmbedder(settings("https://proxy.example.com/compatible-mode/v1"))
    wrong = settings()
    wrong.style_embed_dimension = 768
    with pytest.raises(ValueError, match="1024"):
        BailianStyleEmbedder(wrong)
