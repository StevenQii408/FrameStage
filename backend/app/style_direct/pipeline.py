from __future__ import annotations

import json
import hashlib
import math
import re
import time
from pathlib import Path

from PIL import Image, ImageStat

from app.providers.base import FrameInput
from .models import (DIMENSIONS, TECHNIQUE, EvidenceClaim, EvidenceGap, GenerationConstraint,
                     InterpretationClaim, StyleEvidenceV3, StyleInterpretationV3,
                     StyleProfileV3, StyleAnchor, StyleReference, StyleVerificationV1, RecipeRecommendation,
                     render_profile, safe_visual_text, visible_clauses, RENDERER_VERSION)
from .anchors import candidates, catalog, label, digest as anchor_digest, verify_signature_support
from .library import MATCHER_VERSION, runtime_library
from .prompts import COMMON, EVIDENCE_PROMPT, EVIDENCE_RECOVERY_PROMPT, INTERPRET_PROMPT, SUPPLEMENT_PROMPT, VERIFICATION_PROMPT, VERSION
from .consistency import check_consistency, VERSION as CONSISTENCY_VERSION
from .diagnostics import build_diagnostics, LABELS
from .reliability import feature_quality, resolution, VERSION as RELIABILITY_VERSION
from .evidence import normalize_claim, global_claim, grounded_name, INFERENCE, VERSION as EVIDENCE_SCOPE_VERSION
from .references import review_candidates, verify_reference, reference_label, reference_digest, VERSION as REFERENCE_VERSION
from .recommendations import verify_recommendation, recommendation_cards, VERSION as RECOMMENDATION_VERSION
from app.runtime import (check_execution, ExecutionCancelled, ExecutionContext,
                         current_execution, execution_scope, execution_deadline)


class StylePipeline:
    """One bounded acquisition round; all HTTP calls share a deadline and budget."""
    def __init__(self, provider, assets: list[dict], output_dir: Path, *, scope="reference", notify=None,
                 timeout=180, clock=time.monotonic, cancelled=None):
        self.provider, self.assets, self.output_dir = provider, list(assets), output_dir
        self.scope, self.notify, self.clock = scope, notify or (lambda stage, detail: None), clock
        self.cancelled = cancelled or (lambda: False)
        self.deadline = clock() + timeout
        self.raw, self.audit, self.tools, self.notes = [], [], [], []
        self.calls, self.correction_used = 0, False
        self.images = {asset["alias"]: asset for asset in assets}
        self.anchor_candidates = []
        self.validation_seconds = self.retrieval_seconds = self.render_seconds = 0.0
        self.stages, self.current_stage = [], None
        self.phase_validation = {}
        self.repaired_claim_ids = set()
        self.supplement_rounds = 0
        self.reference_rejections = []

    def close_stage(self):
        if self.current_stage is None:
            return
        stage, started, call_start, audit_start = self.current_stage
        rows = self.raw[call_start:]
        failed = any(r.get('error') or r.get('validation_status') == 'failed' for r in rows)
        recovered = any(r.get('validation_status') in ('valid', 'partial') for r in rows)
        stage.update(elapsed_seconds=round(max(0, self.clock() - started), 4), model_requests=len(rows),
                     status="failed" if failed and not recovered else
                     "partial" if failed or any(a.get('level') == 'warning' for a in self.audit[audit_start:]) else "completed")
        self.current_stage = None

    def stage(self, name, detail):
        self.check(check_deadline=False)
        self.close_stage()
        row = {"name": name, "label": LABELS.get(name, name), "elapsed_seconds": 0,
               "model_requests": 0, "status": "running"}
        self.stages.append(row)
        self.current_stage = (row, self.clock(), len(self.raw), len(self.audit))
        self.notify(name, detail)

    def call(self, prompt, schema, phase, aliases, limit=60):
        self.check()
        remaining = self.deadline - self.clock()
        if self.calls >= 4 or remaining <= 0:
            raise TimeoutError("画风分析调用或时间预算耗尽")
        self.calls += 1
        entry = {"phase": phase, "images": aliases, "schema": schema.__name__, "status": "running",
                 "prompt_characters": len(prompt), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        self.raw.append(entry)
        frames = [FrameInput(alias, self.images[alias].get("timestamp", 0), self.images[alias]["path"])
                  for alias in aliases]
        started = self.clock()
        if self.current_stage:
            stage = self.current_stage[0]
            self.notify(stage['name'], f"{stage['label']}：第{self.calls}/4次请求，本次最多{min(limit, remaining):.0f}秒")
        try:
            execution = ExecutionContext(parent=current_execution(), cancelled=self.cancelled,
                                         deadline=execution_deadline(min(limit, remaining)))
            with execution_scope(execution):
                entry["response"] = self.provider.analyze_style(
                    prompt, frames, phase=phase, schema=schema,
                    timeout_seconds=min(limit, remaining),
                    max_tokens=4200 if schema is StyleVerificationV1 else 3200 if schema is StyleInterpretationV3 else 2400)
            self.check()
            entry['status'] = 'completed'
            return entry["response"]
        except Exception as exc:
            entry['status'] = 'failed'
            entry["error"] = str(exc)[:250]
            raise
        finally:
            entry["elapsed_seconds"] = round(self.clock() - started, 3)

    def check(self, *, check_deadline=True):
        check_execution()
        if self.cancelled():
            raise ExecutionCancelled("画风任务已取消，不再执行后续请求")
        if check_deadline and self.clock() >= self.deadline:
            raise TimeoutError("画风分析总时间预算耗尽")

    def parse(self, raw, schema, claims=None):
        started = self.clock()
        try:
            return self._parse(raw, schema, claims)
        finally:
            self.validation_seconds += max(0, self.clock() - started)

    def _parse(self, raw, schema, claims=None):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
        value = json.loads(raw)
        if schema is StyleVerificationV1:
            if not isinstance(value, dict) or set(value) != {'new_evidence', 'interpretation'}:
                raise ValueError('联合核验必须同时返回新增证据与解释')
            extra = self._parse(json.dumps(value['new_evidence'], ensure_ascii=False), StyleEvidenceV3)
            merged = dict(claims or {})
            for fact in extra.claims:
                if fact.id in merged:
                    raise ValueError('补充证据不能覆盖原事实ID')
                grounded = [c.observation for c in merged.values() if c.dimension == fact.dimension]
                if grounded and not check_consistency(fact.observation, grounded, fact.dimension)['acceptable']:
                    raise ValueError('补充事实与原观察冲突，保留原有效结论')
                merged[fact.id] = fact
            if len(merged) > 30:
                raise ValueError('补充事实超过总预算')
            interpreted = self._parse(json.dumps(value['interpretation'], ensure_ascii=False),
                                      StyleInterpretationV3, merged)
            return StyleVerificationV1(new_evidence=extra, interpretation=interpreted)
        array_key = "claims" if schema is StyleEvidenceV3 else "descriptions"
        if isinstance(value, list):
            value = {array_key: value}
            self.audit.append({"level": "info", "repair": "wrapped-array", "field": array_key})
        if not isinstance(value, dict) or set(value) - set(schema.model_fields):
            raise ValueError("画风输出根结构无效或含未定义字段")
        if array_key not in value or not isinstance(value[array_key], list):
            raise ValueError("画风输出缺少观察/描述数组")
        if not value[array_key] and not value.get("uncertainties"):
            raise ValueError("输出为空且没有不可观察原因；请根据图片返回具体观察，不返回空壳数组")
        children = {"claims": EvidenceClaim, "gaps": EvidenceGap} if schema is StyleEvidenceV3 else {
            "descriptions": InterpretationClaim, "generation_constraints": GenerationConstraint,
            "investigation_gaps": EvidenceGap}
        maximums = {"claims": 30, "gaps": 4, "descriptions": 18, "generation_constraints": 6,
                    "investigation_gaps": 2}
        result = dict(value)
        for field, child in children.items():
            entries = value.get(field, [])
            if not isinstance(entries, list) or len(entries) > maximums[field]:
                raise ValueError("画风字段数量超过预算或不是数组：" + field)
            approved = []
            for i, entry in enumerate(entries):
                try:
                    extra = []
                    item = child.model_validate(entry)
                    if isinstance(item, EvidenceClaim):
                        if item.dimension == "motion_signature":
                            raise ValueError("静态图片不能证明动态画风")
                        if set(item.images) - set(self.images):
                            raise ValueError("引用不存在的图片编号")
                        if item.target and not safe_visual_text(item.target):
                            raise ValueError("衣物、道具或身份属性不能作为画风观察对象")
                        normalized = normalize_claim(item, self.images)
                        inferences = [part.strip() for part in re.split(r'[，；。]', item.observation) if INFERENCE.search(part)]
                        if inferences:
                            result['uncertainties'] = [*result.get('uncertainties', []),
                                *['未将推断视为事实：' + p for p in inferences]][:12]
                        if not normalized:
                            raise ValueError("未证实技术/内容不能进入风格事实")
                        meaningful = lambda text: re.sub(r'[\s，,；;。.]', '', text)
                        if (len(normalized) > 1 or meaningful(normalized[0].observation) != meaningful(item.observation)
                                or normalized[0].evidence_scope != item.evidence_scope):
                            self.repaired_claim_ids.add(item.id)
                            self.audit.append({"field": field, "index": i, "level": "info",
                                "repair": "atomic-scope-and-content-repaired", "source_claim_id": item.id,
                                "retained_claim_ids": [c.id for c in normalized]})
                        item, extra = normalized[0], normalized[1:]
                        if any(prev.id == item.id for prev in approved):
                            raise ValueError("事实ID重复")
                    elif isinstance(item, EvidenceGap):
                        if item.image not in self.images or item.dimension == "motion_signature" or TECHNIQUE.search(item.question):
                            raise ValueError("该疑点无法通过静态原图取证")
                    else:
                        refs = item.support_claim_ids
                        if not claims or set(refs) - set(claims):
                            raise ValueError("专业描述缺少有效事实引用")
                        text = item.professional_description if isinstance(item, InterpretationClaim) else item.text
                        if isinstance(item, GenerationConstraint) and re.search(r'渲染情绪|情绪渲染|暗示|推断', text):
                            self.audit.append({'field': field, 'index': i, 'level': 'info',
                                'repair': 'narrative-inference-not-style-constraint',
                                'reason': '未将情绪推断升级为全局创作约束'})
                            continue
                        if not safe_visual_text(text):
                            if isinstance(item, InterpretationClaim):
                                visible = visible_clauses(text)
                                if not visible:
                                    raise ValueError("专业描述包含技术猜测或内容泄漏")
                                item.professional_description = "，".join(visible)
                                self.audit.append({"field": field, "index": i, "level": "info", "repair": "visible-clauses-preserved"})
                            else:
                                raise ValueError("生成约束包含技术猜测或内容泄漏")
                        if isinstance(item, InterpretationClaim):
                            if set(refs) & self.repaired_claim_ids:
                                # Do not reintroduce removed content or local effects through
                                # a historical B response still pointing to the old paragraph.
                                item.professional_description = '，'.join(dict.fromkeys(claims[key].observation for key in refs))[:300]
                                item.concise_phrase = ''
                            if item.scope == 'global' and re.search(r'仅在|只在|单镜|偶发', item.professional_description):
                                # A B-stage scope declaration cannot add unobserved local facts.
                                item.professional_description = '，'.join(dict.fromkeys(claims[key].observation for key in refs))[:300]
                                item.concise_phrase = ''
                                self.audit.append({'field': field, 'index': i, 'level': 'info',
                                    'repair': 'local-description-restored-from-evidence'})
                            if item.concise_phrase and not safe_visual_text(item.concise_phrase):
                                item.concise_phrase = ""
                            if re.search(r"景深|焦点|焦段|构图|模拟.*光学|摄影镜头", item.professional_description):
                                parts = [part for part in re.split(r"[，；。]", item.professional_description)
                                         if part.strip() and not re.search(r"景深|焦点|焦段|构图|模拟.*光学|摄影镜头", part)]
                                if not parts:
                                    raise ValueError("摄影布局不能进入全局画风")
                                item.professional_description = "，".join(parts)
                                item.concise_phrase = ""  # Rebuild from the remaining visible clauses.
                            if item.dimension == "motion_signature" or any(claims[key].dimension != item.dimension for key in refs):
                                raise ValueError("描述与支持事实维度不一致")
                            grounded = [claims[key].observation for key in refs]
                            long_check = check_consistency(item.professional_description, grounded, item.dimension)
                            if not long_check['acceptable']:
                                # Restore actual observations locally, not another paid rewrite.
                                preserved = []
                                for observation in dict.fromkeys(grounded):
                                    if len('，'.join([*preserved, observation])) <= 300:
                                        preserved.append(observation)
                                item.professional_description = '，'.join(preserved)
                                item.core_concept = item.dimension
                                item.concise_phrase = ''
                                self.audit.append({"field": field, "index": i, "level": "info",
                                    "repair": "description-contradiction-restored-from-evidence",
                                    "reason": "明确控制点与引用观察矛盾，恢复有效观察，不追加请求",
                                    "semantic_check": long_check, "support_claim_ids": refs})
                            if item.concise_phrase:
                                short_check = check_consistency(item.concise_phrase,
                                    [item.professional_description, *grounded], item.dimension, reject_new_controls=True)
                                if not short_check['acceptable']:
                                    item.concise_phrase = ''
                                    self.audit.append({"field": field, "index": i, "level": "info",
                                        "repair": "concise-contradiction-or-new-control-cleared",
                                        "reason": "精简句矛盾或新增无依据控制点，改从有效长描述渲染",
                                        "semantic_check": short_check, "support_claim_ids": refs})
                            images = {alias for key in refs for alias in claims[key].images}
                            # A crop inherits its source frame, not an extra independent supporting shot.
                            origins = {self.images[a].get("parent_alias") or a for a in images}
                            if any(not global_claim(claims[key]) for key in refs):
                                item.scope, item.scene_id = "local", None
                                item.image_alias = next(iter(images)) if len(images) == 1 else None
                                self.audit.append({"field": field, "index": i, "level": "info",
                                    "repair": "local-evidence-not-global", "support_claim_ids": refs})
                            elif self.scope == "reference":
                                item.scope, item.scene_id = "reference", None
                            elif item.scope == "global":
                                originals = [a for a in self.assets if not a.get("parent_alias")]
                                required = max(2, math.ceil(len(originals) * .6)) if len(originals) > 1 else 1
                                if len(origins) < required:
                                    raise ValueError("局部帧特征不能自动晋升全局")
                            elif item.scope == "scene":
                                scene_ids = {self.images[a].get("scene_id") for a in images}
                                if not item.scene_id or scene_ids != {item.scene_id}:
                                    raise ValueError("场次变化引用不属于该场次")
                            elif item.scope == "local":
                                if not item.image_alias or item.image_alias not in images:
                                    raise ValueError("局部效果缺少对应图片引用")
                            else:
                                raise ValueError("项目描述必须明确全局或场次范围")
                        elif self.scope == 'project' and any(not global_claim(claims[key]) for key in refs):
                            raise ValueError("局部证据不能变成全局生成约束")
                    for candidate in [item, *extra]:
                        if len(approved) >= maximums[field]:
                            break
                        if isinstance(candidate, EvidenceClaim) and any(p.id == candidate.id for p in approved):
                            raise ValueError("事实ID重复")
                        approved.append(candidate)
                except (ValueError, TypeError) as exc:
                    self.audit.append({"field": field, "index": i, "level": "warning", "reason": str(exc).splitlines()[0][:180]})
            result[field] = [item.model_dump() for item in approved]
        if not safe_visual_text(result.get("descriptive_name", "")):
            result["descriptive_name"] = "参考画风"
        if schema is StyleInterpretationV3 and result.get("anchor"):
            try:
                anchor = StyleAnchor.model_validate(result["anchor"])
                if anchor.id not in {a['id'] for a in self.anchor_candidates} or anchor.confidence < .85:
                    raise ValueError("锚点非候选或区分置信度不足")
                refs = set(anchor.support_claim_ids)
                if not claims or refs - set(claims) or set(anchor.distinguishing_claim_ids) - refs:
                    raise ValueError("锚点缺少真实区分证据")
                if len({claims[key].dimension for key in refs}) < 2:
                    raise ValueError("锚点至少需要两个不同维度的支持")
                if any(not global_claim(claims[key]) for key in refs):
                    raise ValueError("具体全局锚点不能依赖局部效果")
                verified = verify_signature_support(anchor, claims)
                origins = {self.images[a].get('parent_alias') or a for key in refs for a in claims[key].images}
                if self.scope == 'project' and len(origins) < max(1 if len(self.assets) == 1 else 2, math.ceil(len(self.assets) * .6)):
                    raise ValueError("锚点不能从局部帧晋升全局")
                result['anchor'] = anchor.model_dump()
                self.audit.append({'field': 'anchor', 'level': 'info', 'verified_feature_groups': verified})
            except (ValueError, TypeError) as exc:
                result['anchor'] = None
                self.audit.append({'field': 'anchor', 'level': 'info', 'reason': str(exc).splitlines()[0]})
        if schema is StyleInterpretationV3 and result.get('reference'):
            # Ellipsis is formatting, not invented evidence, only if every part
            # appears in order in the SAME saved fact. Restore the exact span.
            for decision in [result['reference'], *result.get('recommendations', [])]:
                if not isinstance(decision, dict):
                    continue
                for feature in decision.get('matched_features', []):
                    fact = (claims or {}).get(feature.get('claim_id')) if isinstance(feature, dict) else None
                    quote = feature.get('quote', '') if isinstance(feature, dict) else ''
                    if not fact or not re.search(r'\.{3,}|…+', quote):
                        continue
                    pieces = [p.strip() for p in re.split(r'\.{3,}|…+', quote) if p.strip()]
                    cursor, first, valid = 0, None, bool(pieces)
                    for part in pieces:
                        index = fact.observation.find(part, cursor)
                        if index < 0:
                            valid = False
                            break
                        first = index if first is None else first
                        cursor = index + len(part)
                    if valid:
                        feature['quote'] = fact.observation[first:cursor]
                        self.audit.append({'field': 'reference', 'level': 'info', 'repair': 'ellipsis-to-exact-saved-span',
                            'claim_id': fact.id})
            try:
                reference = StyleReference.model_validate(result['reference'])
                refs = verify_reference(reference, claims or {}, self.anchor_candidates)
                origins = {self.images[a].get('parent_alias') or a for key in refs for a in claims[key].images}
                originals = {a['alias'] for a in self.assets if not a.get('parent_alias')}
                if self.scope == 'project' and len(origins & originals) < max(1 if len(originals) == 1 else 2, math.ceil(len(originals) * .6)):
                    raise ValueError('局部参考不能晋升为项目画风')
                if result.get('anchor') and result['anchor']['id'] != reference.id:
                    raise ValueError('严格锚点与视觉参考互相冲突，不采用新参考')
                result['reference'] = reference.model_dump()
                self.audit.append({'field': 'reference', 'level': 'info', 'verified_claim_ids': refs,
                    'selection_basis': reference.selection_basis,
                    'reason': '原图支持的生成视觉参考，不是出品方认定或人工准确率；视觉适配可与近邻重叠'})
            except (ValueError, TypeError) as exc:
                result['reference'] = None
                self.reference_rejections.append(str(exc).splitlines()[0])
                self.audit.append({'field': 'reference', 'level': 'info', 'reason': str(exc).splitlines()[0]})
        if schema is StyleInterpretationV3:
            raw_decisions = value.get('recommendations', [])
            approved, seen = [], set()
            if not isinstance(raw_decisions, list) or len(raw_decisions) > 3:
                self.audit.append({'field': 'recommendations', 'level': 'warning',
                    'reason': '视觉推荐必须为数组且最多三个；不按候选排名补齐'})
                raw_decisions = []
            for entry in raw_decisions:
                try:
                    decision = RecipeRecommendation.model_validate(entry)
                    if decision.id in seen:
                        raise ValueError('推荐配方重复')
                    verify_recommendation(decision, claims or {}, self.anchor_candidates,
                        list(self.images.values()), self.scope)
                    approved.append(decision)
                    seen.add(decision.id)
                except (ValueError, TypeError) as exc:
                    self.audit.append({'field': 'recommendations', 'level': 'info',
                        'reason': str(exc).splitlines()[0]})
            # The explicitly validated primary reference IS a valid option even
            # if the model omits it from recommendations. This does not select
            # any unreviewed retrieval rank or fill the list up to three.
            if result.get('reference') and result['reference']['id'] not in seen:
                primary = RecipeRecommendation.model_validate({**result['reference'], 'fit_level': 'high'})
                verify_recommendation(primary, claims or {}, self.anchor_candidates,
                    list(self.images.values()), self.scope)
                approved = [primary, *approved][:3]
                self.audit.append({'field': 'recommendations', 'level': 'info',
                    'source': 'validated-model-primary-reference', 'reason': '展示模型已核验的首选参考，不用检索排名补齐'})
            result['recommendations'] = [d.model_dump() for d in approved]
            if not result.get('reference') and approved and approved[0].fit_level == 'high':
                first = approved[0]
                if not result.get('anchor') or result['anchor']['id'] == first.id:
                    result['reference'] = first.model_dump(exclude={'fit_level'})
                    self.audit.append({'field': 'reference', 'level': 'info',
                        'source': 'validated-model-first-recommendation',
                        'reason': '采用模型首选的已核验视觉适配方向；high不是准确率'})
            if result.get('reference'):
                # An approved name already cites several global control points.
                # Do not silently lose one of those dimensions just because the
                # B-stage description was omitted or mislabeled as alias-less
                # local. Restore ONLY that reference's actual saved facts.
                covered = {d['dimension'] for d in result['descriptions'] if d['scope'] in ('global', 'reference')}
                originals = {a['alias'] for a in self.assets if not a.get('parent_alias')}
                required = max(2, math.ceil(len(originals) * .6)) if len(originals) > 1 else 1
                for feature in result['reference']['matched_features']:
                    fact = (claims or {}).get(feature['claim_id'])
                    if not fact or fact.dimension in covered or not global_claim(fact):
                        continue
                    origins = {self.images[a].get('parent_alias') or a for a in fact.images}
                    if self.scope == 'project' and len(origins & originals) < required:
                        continue
                    if not safe_visual_text(fact.observation) or len(result['descriptions']) >= 18:
                        continue
                    item = InterpretationClaim(dimension=fact.dimension, core_concept=fact.dimension,
                        professional_description=fact.observation, support_claim_ids=[fact.id],
                        scope='global' if self.scope == 'project' else 'reference')
                    result['descriptions'].append(item.model_dump())
                    covered.add(fact.dimension)
                    self.audit.append({'field': 'descriptions', 'level': 'info',
                        'repair': 'missing-reference-dimension-restored-from-saved-evidence',
                        'support_claim_ids': [fact.id]})
        return schema.model_validate(result)

    def structured(self, prompt, schema, phase, aliases, claims=None):
        def validated(raw):
            before = len(self.audit)
            try:
                value = self.parse(raw, schema, claims)
            except (ValueError, TypeError) as exc:
                self.raw[-1].update(validation_status='failed', validation_error=str(exc)[:200])
                self.phase_validation[phase] = 'failed'
                raise
            status = 'partial' if any(a.get('level') == 'warning' for a in self.audit[before:]) else 'valid'
            self.raw[-1]['validation_status'] = status
            self.phase_validation[phase] = status
            return value

        raw = self.call(prompt, schema, phase, aliases)
        try:
            return validated(raw)
        except (ValueError, TypeError) as exc:
            self.raw[-1]["validation_error"] = str(exc)[:200]
            if self.correction_used or self.calls >= 4 or self.deadline - self.clock() < 10:
                raise
            self.correction_used = True
            self.notify("correcting", "正在纠正输出结构一次，不重新猜测画风")
            correction = prompt + "\n前次输出结构错误：" + str(exc)[:200] + "\n仅纠正该错误，保持事实与图片引用。"
            if schema is StyleEvidenceV3 and "输出为空" in str(exc):
                correction = EVIDENCE_RECOVERY_PROMPT + "\n实际图片目录：" + json.dumps([
                    {k: self.images[a].get(k) for k in ('alias', 'scene_id', 'timestamp', 'background_policy')}
                    for a in aliases], ensure_ascii=False)
            # Include original visual evidence, never pure-text hallucination repair.
            raw = self.call(correction, schema, "style.direct.correction", aliases, limit=30)
            self.raw[-1]['parent_phase'] = phase
            self.raw[-1]['correction_strategy'] = 'empty-evidence-reobserve' if correction.startswith(EVIDENCE_RECOVERY_PROMPT) else 'structural-repair'
            return validated(raw)

    def acquire(self, gaps):
        """Native crops only. Two regions total; no super-resolution or arbitrary paths."""
        started = self.clock()
        added = []
        self.output_dir.mkdir(parents=True, exist_ok=True)
        for gap in gaps:
            if len(added) >= 2 or self.deadline - self.clock() < 30:
                break
            source = self.images[gap.image]
            record = {"tool": gap.tool, "dimension": gap.dimension, "question": gap.question,
                      "image": gap.image, "regions": gap.regions, "expected_evidence": gap.expected_evidence,
                      "results": [], "errors": []}
            self.tools.append(record)
            with Image.open(source.get("original_path") or source["path"]) as original:
                original = original.convert("RGB")
                for box in gap.regions:
                    if len(added) >= 2:
                        break
                    if (box[2] - box[0]) * (box[3] - box[1]) >= .95:
                        record['errors'].append('整张重复图片没有新证据，未追加请求')
                        continue
                    if any(a.get('parent_alias') == gap.image and a.get('bbox') == box for a in self.images.values()):
                        record['errors'].append('同一区域已核验，不重复取证')
                        continue
                    xy = (int(box[0] * original.width), int(box[1] * original.height),
                          int(box[2] * original.width), int(box[3] * original.height))
                    if xy[2] - xy[0] < 16 or xy[3] - xy[1] < 16:
                        record["errors"].append("区域像素不足，未放大或补造细节")
                        continue
                    crop = original.crop(xy)
                    alias = "I" + str(len(self.images) + 1)
                    path = self.output_dir / (alias + ".jpg")
                    native = self.output_dir / (alias + "-original.png")
                    crop.save(native)
                    crop.thumbnail((1536, 1536))
                    crop.save(path, quality=92)
                    asset = {"alias": alias, "asset_id": source["asset_id"] + "-" + alias,
                             "path": str(path), "original_path": str(native), "width": crop.width,
                             "height": crop.height, "parent_alias": gap.image, "bbox": box,
                             "scene_id": source.get("scene_id"), "timestamp": source.get("timestamp", 0)}
                    self.images[alias] = asset
                    added.append(alias)
                    result = {"alias": alias, "width": crop.width, "height": crop.height, "bbox": box}
                    if gap.tool == "compare_color":
                        stats = ImageStat.Stat(crop)
                        result["mean_rgb"] = [round(v, 2) for v in stats.mean]
                        result["stddev_rgb"] = [round(v, 2) for v in stats.stddev]
                    record["results"].append(result)
            record["elapsed_seconds"] = round(self.clock() - started, 3)
        return added

    def supplement(self, evidence, gaps):
        """One evidence acquisition round across A and B; no free-form agent loop."""
        if self.supplement_rounds or self.correction_used or self.calls > 2 or self.deadline - self.clock() < 90:
            return False
        self.supplement_rounds = 1
        self.stage('acquiring', '针对缺失的区别性特征读取原图，不重复猜测风格')
        added = self.acquire(gaps)
        if not added:
            self.audit.append({'field': 'investigation', 'level': 'info',
                               'reason': '没有新增可观察证据，停止补充调用'})
            return False
        aliases = list(dict.fromkeys(self.images[a]['parent_alias'] for a in added)) + added
        self.stage('verifying', '核验新增局部证据一次')
        try:
            result = self.structured(SUPPLEMENT_PROMPT + '\n原事实：' + evidence.model_dump_json() +
                '\n待核验问题：' + json.dumps([g.model_dump() for g in gaps], ensure_ascii=False) +
                '\n工具结果：' + json.dumps(self.tools, ensure_ascii=False),
                StyleEvidenceV3, 'style.direct.supplement', aliases)
            seen = {c.id for c in evidence.claims}
            for claim in result.claims:
                if claim.id in seen:
                    number = 1
                    while 'S' + str(number) in seen:
                        number += 1
                    claim.id = 'S' + str(number)
                if len(evidence.claims) < 30:
                    evidence.claims.append(claim)
                    seen.add(claim.id)
            evidence.uncertainties.extend(result.uncertainties[:4])
            return bool(result.claims)
        except Exception as exc:
            if 'HTTP 401' in str(exc) or 'HTTP 403' in str(exc):
                raise
            self.notes.append('补证据未完成，基础观察已保留：' + str(exc)[:150])
            return False

    def interpret(self, evidence, originals, catalogue, *, phase='style.direct.interpretation'):
        claims = {c.id: c for c in evidence.claims if c.confidence >= .5}
        self.stage('retrieving', '召回相关候选；文字门槛不等于视觉相似度')
        started = self.clock()
        self.anchor_candidates = candidates(list(claims.values()))
        self.retrieval_seconds += max(0, self.clock() - started)
        self.stage('interpreting', '根据原图、区别性证据与反例裁决画风')
        aliases = self.review_aliases(originals) if self.tools else originals
        return self.structured(INTERPRET_PROMPT + '\n范围：' + self.scope +
            '\n已核验事实：' + json.dumps([c.model_dump() for c in claims.values()], ensure_ascii=False) +
            '\n图片目录：' + json.dumps(catalogue, ensure_ascii=False) +
            '\n视觉参考候选（排名仅用于召回、非来源鉴定）：' + json.dumps(review_candidates(self.anchor_candidates), ensure_ascii=False),
            StyleInterpretationV3, phase, aliases, claims)

    def review_aliases(self, originals):
        crops = [a for a in self.images if a not in originals][:2]
        context = list(dict.fromkeys(self.images[a]['parent_alias'] for a in crops))
        for alias in originals:
            if len(context) >= 4 - len(crops):
                break
            if alias not in context:
                context.append(alias)
        return context + crops

    def verify_once(self, evidence, interpretation, originals, catalogue):
        """After correction, one remaining call can observe crops AND decide.

        Normal two-call acquisition/refinement stays compatible. No request is
        made when the tool returned no new evidence or the total budget is spent.
        """
        if self.supplement_rounds or self.calls >= 4 or self.deadline - self.clock() < 45:
            self.audit.append({'field': 'investigation', 'level': 'info', 'reason': '本轮补证据或总预算已用尽，保留已知特征'})
            return interpretation
        if self.calls <= 2 and not self.correction_used and self.deadline - self.clock() >= 90:
            if self.supplement(evidence, interpretation.investigation_gaps) and self.calls < 4:
                return self.interpret(evidence, originals, catalogue, phase='style.direct.refinement')
            return interpretation
        self.supplement_rounds = 1
        self.stage('acquiring', '按剩余预算定向读取原图，不因格式纠错关闭调查')
        added = self.acquire(interpretation.investigation_gaps)
        if not added:
            self.audit.append({'field': 'investigation', 'level': 'info', 'reason': '没有新增可观察证据，停止补充调用'})
            return interpretation
        self.stage('verifying', '一次联合核验新增证据与相近候选，最多四次总调用')
        claims = {c.id: c for c in evidence.claims if c.confidence >= .5}
        # Preserve original image context for crops; avoid treating a crop as
        # another independent supporting frame.
        aliases = self.review_aliases(originals)
        payload = {'facts': [c.model_dump() for c in claims.values()], 'scope': self.scope,
            'questions': [g.model_dump() for g in interpretation.investigation_gaps],
            'candidates': review_candidates(self.anchor_candidates),
            'images': [{k: a.get(k) for k in ('alias', 'parent_alias', 'bbox', 'scene_id')} for a in self.images.values()]}
        review = self.structured(VERIFICATION_PROMPT + '\n核验资料：' + json.dumps(payload, ensure_ascii=False),
                                StyleVerificationV1, 'style.direct.verification', aliases, claims)
        if len(evidence.claims) + len(review.new_evidence.claims) > 30:
            raise ValueError('补充事实超过总预算，保留第一次有效画风')
        evidence.claims.extend(review.new_evidence.claims)
        evidence.uncertainties = list(dict.fromkeys([*evidence.uncertainties, *review.new_evidence.uncertainties]))[:12]
        return review.interpretation

    def run(self):
        started = self.clock()
        originals = list(self.images)
        evidence = StyleEvidenceV3()
        interpretation = StyleInterpretationV3()
        # Expose only canonical aliases and scene IDs, never filesystem paths to the model.
        catalogue = [{k: a.get(k) for k in ("alias", "width", "height", "scene_id", "timestamp", "background_policy")} for a in self.assets]
        self.stage("observing", "提取具体视觉证据")
        try:
            evidence = self.structured(EVIDENCE_PROMPT + "\n图片目录：" + json.dumps(catalogue, ensure_ascii=False),
                                       StyleEvidenceV3, "style.direct.evidence", originals)
            if evidence.gaps and not self.correction_used:
                self.supplement(evidence, evidence.gaps)
            claims = {item.id: item for item in evidence.claims if item.confidence >= .5}
            if claims:
                interpretation = self.interpret(evidence, originals, catalogue)
                # Local validation can expose a genuine evidence gap even when
                # the model reports gaps=[]. Investigate once, not a blind retry
                # and never rescue a conflicting medium or invent a studio name.
                if (not interpretation.investigation_gaps and not interpretation.reference
                        and self.reference_rejections and not self.supplement_rounds
                        and any('具体证据' in reason or '控制点' in reason for reason in self.reference_rejections)):
                    fact = next((c for c in evidence.claims if global_claim(c)
                                 and c.dimension in ('lighting_shading', 'texture_material')), None)
                    dimension = fact.dimension if fact else 'lighting_shading'
                    image = fact.images[0] if fact else originals[0]
                    interpretation.investigation_gaps = [EvidenceGap(
                        dimension=dimension, image=image, tool='crop_image', regions=[[.1, .05, .9, .95]],
                        question='根据新增原分辨率区域确认阴影边界、体积层级或表面简化；与原图对照，不补造任何技术或品牌。',
                        expected_evidence='带图片引用和visual_aspects的可见控制点；证据仍不足时拒绝命名')]
                    self.audit.append({'field': 'investigation', 'level': 'info',
                        'reason': '本地参考核验发现证据缺口，开启一次定向调查，而非按候选排名回退'})
                if not interpretation.investigation_gaps and self.correction_used and evidence.gaps:
                    interpretation.investigation_gaps = evidence.gaps[:2]
                if interpretation.investigation_gaps and not (interpretation.anchor or interpretation.reference):
                    try:
                        interpretation = self.verify_once(evidence, interpretation, originals, catalogue)
                    except Exception as exc:
                        self.notes.append('补充裁决未完成，保留第一次有效画风：' + str(exc)[:150])
            else:
                self.notes.append("缺少可靠视觉事实，未生成推测性画风")
        except Exception as exc:
            self.notes.append("画风阶段未完整完成：" + str(exc)[:200])
        self.stage("rendering", "校验并渲染具体视觉语言")
        render_started = self.clock()
        rejected_fields = sorted({a["field"] for a in self.audit
                                  if a.get("level") == "warning" and a.get("field") != "gaps"})
        if rejected_fields:
            self.notes.append("部分观察或描述未通过证据校验，已保留其他有效特征：" + "、".join(rejected_fields))
        if not interpretation.descriptions:
            # Honest partial fallback, not a fake professional expansion.
            originals_set = {a['alias'] for a in self.assets if not a.get('parent_alias')}
            required = max(2, math.ceil(len(originals_set) * .6)) if len(originals_set) > 1 else 1
            retained = []
            for c in evidence.claims:
                origins = {self.images[a].get('parent_alias') or a for a in c.images}
                if c.confidence >= .5 and global_claim(c) and (self.scope == 'reference' or len(origins & originals_set) >= required):
                    retained.append(InterpretationClaim(dimension=c.dimension, core_concept=c.dimension,
                        professional_description=c.observation, support_claim_ids=[c.id],
                        scope='reference' if self.scope == 'reference' else 'global').model_dump())
            if retained:
                fallback = self.parse(json.dumps({'descriptions': retained[:18]}, ensure_ascii=False),
                                      StyleInterpretationV3, {c.id: c for c in evidence.claims})
                interpretation.descriptions = fallback.descriptions
                interpretation.anchor = None
                interpretation.reference = None
                interpretation.generation_constraints = []
            self.notes.append("专业转译未完成，仅保留基础观察")
        specificity = feature_quality(interpretation.descriptions)
        if interpretation.descriptions and not specificity['sufficient']:
            self.notes.append("区分性特征不足，具体画风未完全确定，未强制命名或追加请求")
        known_family = grounded_name(interpretation.descriptions, interpretation.descriptive_name)
        profile = StyleProfileV3(reference_scope=self.scope, descriptive_name=known_family or interpretation.descriptive_name,
            anchor=interpretation.anchor,
            reference=interpretation.reference,
            descriptions=interpretation.descriptions, generation_constraints=interpretation.generation_constraints,
            uncertainties=list(dict.fromkeys([*evidence.uncertainties, *interpretation.uncertainties, *self.notes]))[:24],
            application_status="ready" if interpretation.descriptions and not self.notes else "partial",
            provenance={"model": self.provider.health().model, "prompt_version": VERSION,
                        "strategy": "qwen-direct-v3", "motion_observable": False,
                        "anchor_catalog_version": catalog()['version'], "anchor_catalog_hash": anchor_digest(),
                        "knowledge_source_hash": runtime_library().source_hash,
                        "anchor_matcher_version": MATCHER_VERSION,
                        "consistency_version": CONSISTENCY_VERSION,
                        "reliability_version": RELIABILITY_VERSION,
                        "evidence_scope_version": EVIDENCE_SCOPE_VERSION,
                        "renderer_version": RENDERER_VERSION})
        profile.provenance['reference_review_version'] = REFERENCE_VERSION
        profile.provenance['reference_cards_hash'] = reference_digest()
        profile.provenance['recommendation_version'] = RECOMMENDATION_VERSION
        if profile.reference:
            profile.descriptive_name = reference_label(profile.reference.id)
            recipe = next(r for r in runtime_library().recipes if r.id == profile.reference.id)
            compared = {a.id for a in profile.reference.alternative_assessments}
            unreviewed = sorted(set(recipe.confusable_with) - compared)
            profile.provenance['reference_comparison_coverage'] = {
                'mode': profile.reference.selection_basis, 'reviewed_ids': sorted(compared),
                'unreviewed_nearby_ids': unreviewed,
                'exclusive_classification': profile.reference.selection_basis == 'distinctive',
            }
            if unreviewed:
                profile.uncertainties.append('已确认非排他的创作视觉适配，尚未逐一完成全部近邻区分，不能据此认定原片来源。')
        render_profile(profile)
        validations = [r.get('validation_status') for r in self.raw if r.get('validation_status')]
        structure_status = ('failed' if validations and all(v == 'failed' for v in validations) else
                            'partial' if 'failed' in validations or rejected_fields else
                            'valid' if validations else 'unknown')
        quality_state = {
            'structure': structure_status,
            'observation': 'available' if evidence.claims else 'unknown',
            'interpretation': 'completed' if self.phase_validation.get('style.direct.refinement', self.phase_validation.get('style.direct.interpretation')) == 'valid'
                and interpretation.descriptions and not any('专业转译未完成' in n for n in self.notes) else
                'partial' if interpretation.descriptions else 'failed',
            'style': resolution(profile), 'anchor': 'supported' if profile.anchor else 'unknown',
            'reference': 'supported' if profile.reference else 'unknown',
            'specificity': specificity,
            'descriptor': 'usable' if any(d.scope in ('reference', 'global') for d in interpretation.descriptions) else 'unknown',
            'family': 'supported' if known_family and specificity['sufficient'] else 'partial' if interpretation.descriptions else 'unknown',
            'anchor_validation': 'model-grounded-quotes' if profile.anchor and any(g.semantic_support for g in profile.anchor.feature_support)
                else 'literal-signature' if profile.anchor else 'not-established',
        }
        profile.provenance['quality_state'] = quality_state
        profile.provenance['resolution_status'] = quality_state['style']
        profile.profile_hash = profile.digest()
        if profile.anchor:
            profile.descriptive_name = label(profile.anchor.id)
            profile.profile_hash = profile.digest()
        self.render_seconds = max(0, self.clock() - render_started)
        self.close_stage()
        result = {"evidence": evidence.model_dump(), "interpretation": interpretation.model_dump(),
                "recommendations": recommendation_cards(interpretation.recommendations),
                "profile": profile.model_dump(), "assets": list(self.images.values()), "raw_responses": self.raw,
                "candidate_snapshot": self.anchor_candidates,
                "quality_state": quality_state,
                "audit": self.audit, "tools": self.tools,
                "budget": {"model_requests": self.calls, "max_model_requests": 4,
                           "supplement_rounds": self.supplement_rounds, "crop_count": len(self.images) - len(originals),
                           "correction_used": self.correction_used, "elapsed_seconds": round(self.clock() - started, 3)}}
        result['diagnostics'] = build_diagnostics(result, stages=self.stages, validation_seconds=self.validation_seconds,
            retrieval_seconds=self.retrieval_seconds, render_seconds=self.render_seconds)
        return result
