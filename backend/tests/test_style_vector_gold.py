import hashlib

import pytest
from pydantic import ValidationError

from app.style_vector.ontology import CATEGORIES, terms
from evals.style_vector_gold import StyleVectorGoldSet, score, verify_media


CONTENT_CASES = [
    "黑发少女", "卷发女孩", "白色裙子", "蓝色衣服", "棉质外套", "红色鞋子",
    "木质桌子", "玻璃杯", "银色戒指", "橙色饮料", "厨房餐桌", "卧室灯光",
    "男孩的卷发", "女人的发型", "男人的皮鞋", "女孩的围巾", "桌面上的杯子",
    "戒指上的宝石", "一只黑色鞋子", "橙汁与玻璃杯", "衣服上的蓝条纹",
    "红裙女孩", "男人走进厨房", "女人拿起道具", "男孩端着饮料",
    "白色棉质裙子", "卷发少年的衣服", "厨房里的木桌子", "银戒指特写",
    "街道上的黑发女孩",
]


def sample(index, category, term_id, digest):
    return {
        "sample_id": f"human-{index:03d}", "source": "human", "reviewer": "测试标注员",
        "reviewed_at": "2026-09-29", "rights": "owned", "rights_reference": "自有测试图",
        "media_file": "frame.png", "sha256": digest, "category": category,
        "query": terms()[term_id].name_cn, "expected_term_ids": [term_id],
        "expected_scope": ("GLOBAL", "SCENE", "SHOT", "UNCERTAIN")[index % 4],
        "scene_id": "scene-1", "evidence": [{"frame_id": "F1", "timestamp": 0.0, "description": "人工可见特征"}],
    }


def test_v2_human_gold_and_metrics(tmp_path):
    image = tmp_path / "frame.png"
    image.write_bytes(b"authored fixture")
    digest = hashlib.sha256(image.read_bytes()).hexdigest()
    by_category = {category: next(term.id for term in terms().values() if term.category == category)
                   for category in CATEGORIES}
    cases = [sample(i, CATEGORIES[i % len(CATEGORIES)], by_category[CATEGORIES[i % len(CATEGORIES)]], digest)
             for i in range(60)]
    gold = StyleVectorGoldSet(samples=cases, adversarial_content=CONTENT_CASES)
    verify_media(gold.samples[0], tmp_path)
    predicted = {item.sample_id: {"candidate_ids": item.expected_term_ids, "scope": item.expected_scope}
                 for item in gold.samples}
    report = score(gold, predicted)
    assert report["recall_at_5"] == 1
    assert report["mrr"] == 1
    assert report["scope_macro_f1"] == 1
    assert report["global_false_promotion_rate"] == 0
    assert report["adversarial_content_leakage_rate"] == 0
    with pytest.raises(ValidationError):
        StyleVectorGoldSet(samples=cases[:59], adversarial_content=CONTENT_CASES)
    with pytest.raises(ValidationError):
        StyleVectorGoldSet(samples=cases, adversarial_content=CONTENT_CASES[:29])
