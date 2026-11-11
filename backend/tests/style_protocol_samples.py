"""Original synthetic provider payloads. No saved jobs, media, or cloud responses."""
from copy import deepcopy


def _claim(key, dim, text, images=("I1", "I2", "I3")):
    return dict(id=key, dimension=dim, observation=text, images=list(images), confidence=.9)


def _description(c):
    return dict(dimension=c["dimension"], core_concept=c["dimension"],
                professional_description=c["observation"], support_claim_ids=[c["id"]], scope="global")


def reference_pair(*, contrastive=False):
    a = {"claims": [
        _claim("E1", "medium_rendering", "三维动画，圆柱体从亮区到暗区保持连续体积；并非手绘动画"),
        _claim("E2", "form_language", "夸张比例，眼部放大，发束由块面化几何体组成"),
        _claim("E3", "color_tone", "冷暖分离，暗青与浅黄明度对照；肤色随着情绪泛红"),
        _claim("E4", "lighting_shading", "阴影边缘柔和过渡，亮面高光集中，暗面仍有层次"),
    ], "gaps": []}
    desc = [_description(c) for c in a["claims"]]
    desc[2]["professional_description"] = "暗青与浅黄形成冷暖分离"
    ref = dict(id="pixar-character-3d", confidence=.9,
               matched_features=[dict(claim_id=c["id"], quote=c["observation"].split("；")[0],
                                      relevance="该特征可用作受证据约束的视觉参考。")
                                 for c in a["claims"] if c["id"] in ("E1","E2","E4")],
               distinguishing_claim_ids=["E2","E4"],
               reason="可以复用块面发束与阴影宽度的组合，不推断作品出处。",
               alternative_assessments=[dict(id=key, verdict="indistinguishable" if i == 0 else "distinguishable",
                        support_claim_ids=["E2","E4"], difference="两种造型都容许放大眼部；阴影边界与发束分块可作比较。")
                        for i,key in enumerate(("disney-modern-3d", "photoreal-cgi"))])
    if not contrastive:
        ref["selection_basis"] = "visual_fit"
    b = dict(descriptive_name="夸张比例的三维角色动画", descriptions=desc, reference=ref,
             generation_constraints=([dict(text="保留块面化几何体形成的发束。", support_claim_ids=["E2"])] if contrastive else []))
    return a,b


def stage_failure():
    c = [_claim("M","medium_rendering","连续体积的三维角色动画"),
         _claim("F","form_language","头部偏大，身体轮廓由圆弧连接"),
         _claim("L","lighting_shading","阴影边缘沿体积缓慢过渡"),
         _claim("C","color_tone","暗部偏紫，亮区偏黄形成冷暖对照"),
         _claim("T","texture_material","表面反射宽度按区域分区")]
    return {"responses":["[]",dict(claims=c),"[]"]}


def density_pair():
    a,b=reference_pair()
    a["claims"][1]["observation"]="夸张头部比例，眼睛占据面部较大面积，发束划分为大块的卷曲体块"
    a["claims"][3]["observation"]="柔和漫射，阴影边缘模糊，高光沿转折处逐步减弱"
    a["claims"].append(_claim("W","texture_material","仅I3可见汗水与湿润反光；暗示情绪变化", ("I3",)))
    b.update(descriptive_name="柔和漫射三维角色", reference=None, generation_constraints=[],
             descriptions=[_description(c) for c in a["claims"] if c["id"] in ("E1","E2","E4")])
    return {"responses":[a,b]}


def montage_case():
    from test_event_sequences import shot, relation
    shots = [shot(i, i-1, 1, initial="握住把手", final="握住把手", action="轻轻转动把手") for i in range(1,9)]
    for s in shots:
        s.id = f"synthetic-shot-{s.number:02d}"
    edges = [relation(left,right, role="independent-reaction" if right.number == 8 else "continuation")
             for left,right in zip(shots,shots[1:])]
    return dict(shots=[s.model_dump() for s in shots],montage=dict(relations=edges),style_responses=list(reference_pair()))


def wire_case():
    case = montage_case()
    pairs = case["montage"]["relations"]
    revised = deepcopy(pairs)
    # The quoted alias is not present. Only a literal value on the actual shot can restore it.
    r = revised[3]
    r["context_evidence"] = [
        dict(kind="location", value="厨房", shot_id=r["left_shot_id"], source_field="scene", quote="不存在的别称"),
        dict(kind="prop", value="杯子", shot_id=r["right_shot_id"], source_field="objects", quote="杯子")]
    a,b=reference_pair()
    b["descriptions"][3]["professional_description"] += "（高光沿曲面变化）"
    return dict(style_responses=[a,b], montage_responses=[pairs,dict(relations=revised)])
