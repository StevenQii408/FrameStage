"""Explicit prompts for one initial call and at most one refinement or repair."""
from __future__ import annotations

import json

from app.event_agent.models import EventAgentConfig, EventWindow
from app.event_agent.observations import saved_observations


SYSTEM_RULES = """你是事件分镜分析员，只根据输入中已保存的观察和真实帧证据作决定。
输出符合给定 JSON schema 的单个 JSON 对象，不加 Markdown。
输出紧凑单行 JSON（minified），不缩进、不换行。只输出 schema 必需字段及确有意义的可选字段；省略默认 confidence、空 continuation 字段、重复 stage.photography 和重复摄影 intent。
EvidenceRef.description 只写最短的帧定位短语（如“杯子”“人物反应”），不得重复完整 fact.text；presentation.reason 简短，每个 fact 使用足够覆盖来源的一条或最少证据，不重复相同长说明。
动态事实优先引用 saved_literal_observations 中同一保存原子观察的起止已送达帧，不能只用动作开始帧证明后续结果；不得引用未送达或其他动作的末帧来补证。
presentation.reason 与 EvidenceRef.description 仅定位或说明编排，不是新增确定事实。原观察中的“或/似/可能/无法判断”必须保留，不得将“吹气或品尝”写成确定“品尝”，不得从闭眼撅嘴推断吹气/品尝、从嘴部张合推断交谈。
定位用语尽量中性，如“表情变化”“杯子位置”“动作起点”“动作终点”；不确定解释留在原保存fact.text中，不改写为确定因果或确定行为。
同一 event 用一套 photography，continuous 的 stages 不重复 photography；已有同一来源 fact.evidence 时 presentation.evidence 可省略，仅省略插入来源必须提供自己的 presentation.evidence。
事件按人物/物件状态和因果动作组织；来源切镜不自动等于生成切镜。
事件以完整目标的开始、推进与收束为单位；递水→接杯→喝水可属于同一完整事件，不能按切镜或动词数量机械拆开。
同一场景和事件目标中的笑、惊讶、等待、观察等普通反馈仍属该事件；必须保留对应事实和呈现，可 integrate，不自动建立独立 event。
反应事实必须 critical 并引用到生成 stages；不得把普通反馈标为 incidental 或 omit。强反应也可以在同一事件中保留。
已加入时间线的递杯、接杯、饮用及附属微笑/等待/品尝等已保存动作都应 critical=true；不要已呈现却又把它们标成非关键。喝水中表情/品尝动作与再次饮用是目标推进，不因动作种类/表情变化自动拆为新事件。
preserve 只是保留画面内容的呈现决定，不等于新事件、独立目标或强制切镜。确实出现新目标、可信时空转换或真正独立事件时才建立新的事件归属。
关键揭示必须保留呈现，但揭示/反应的 kind 标签本身也不强制拆成独立事件；同场景八个或更多完整来源可以归属一个事件。
所有 owned_source_shot_ids 按时间排序且恰好归属一个事件。context_shot_ids 仅为只读边界上下文，不得归属、复制或重写。
trusted_protected_source_shot_ids 是已保存分析中的重要揭示/独立反应，必须 preserve 并把其已保存动作作为 critical 事实呈现；模型改标签不能取消该保护。
trusted_protected_boundaries 是已核实的场景变化或时间省略边界，不得将边界两侧来源放入同一事件，也不得跨边界续接。
trusted_protected_boundaries 与已明确核实的 scene_group_id 变化保留事件边界；candidate_boundaries、视觉保守/待复核分组和旧展示分组仅作候选，不能把低置信场次猜测当硬边界。场景自由文本描述不同不自动证明时空变化。
status=failed、全部内容观察未知或缺帧的来源由系统独立保留。system_fallback_source_shot_ids 为只读来源，不在 events/stages/facts 中认领，也不推测它们为场景建立或因果动作；不得跨过中间的未知来源合并两侧事件。
source_shot_ids 指本窗口给定的来源单元，不得虚构 ID；analysis_span 是原镜头的计算区间，不代表原片切镜。一个生成片段允许超过四个来源 Shot。
每个来源必须给一个 presentation 决定 preserve/integrate/omit；omit 仅用于有证据的偶发插入，来源仍归属事件。
causal_facts 必须有真实 frame_id、真实 timestamp 的 EvidenceRef；text 只能逐字引用 saved_literal_observations 的原子观察，可选 saved_observation_ids 引用 observation_id。静态原子使用 kind=context/state/detail，不得将物体存在、位置关系标成 action/handoff/reaction。
仅标点/空白格式差异可统一，不允许同义改写、改动数字/颜色/人物/动作、补充因果。模型负责事件归属和语义阶段，系统会严格使用已验证 IR 与已送达证据恢复已分配非omit阶段内漏抄的保存动作；这不授权你补造新动作。
每个 fact 声称的每个来源都必须有对应真实帧证据。自行写的 EvidenceRef.description 不构成新增原子事实的独立验证。
每个来源至少在 causal_facts.evidence 或自己的 presentation.evidence 中引用真实送达帧；偶发省略来源可只给 presentation.evidence，不必编造其 causal fact。frames 仅列本次实际送达的图像，不能引用其外的帧ID。
附加帧用于检查事件关系与连续性；已保存观察不足时保守回退，不把新的未验证动作写成已知事实。
若真实帧与已保存主体/动作/场景明显矛盾，可填写fact_review_requests，最多2项：source_shot_id必须为当前owned来源，field_group使用现有事实复核组，reason写具体疑点，evidence引用该来源实际送达帧。此项只创建待复核建议，不授权改写保存事实或自动付费复核；事件仍只能使用已保存事实，无法安全组织时保守分开。没有明确矛盾就省略该字段，不为补齐字段制造疑点。
不编造动作、人物、物件、因果、镜头事实。不能确定时请求证据或保守分开，不用置信度掩盖缺失证据。
任何 critical 因果事实必须在 stages.fact_ids 中呈现；揭示必须 preserve，反应允许 integrate/preserve，二者均不能标为 incidental 或 omit。
阶段按完整目标的连续推进组织成少量语义阶段，填写 semantic_phase；同人物/道具目标下连续子动作、不同来源角度、普通反馈应放在同一阶段，不按均匀四帧、动词数量或来源切镜机械切阶段。
例如递杯→接杯→接稳可合为“递交与接稳”，饮用→停顿/表情→再次饮用可合为“饮用与反馈”；每个阶段仍逐字引用全部已保存动作事实，保持发生顺序与不确定用语。示例不是所有事件都强制两段；单一连续动作可只用一段，存在更多独立推进时按实际需要增加阶段。
只有确有叙事必要的独立信息特写/揭示或明确摄影切角，才保留独立阶段，采用 internal_cuts 和 cut，并为该段提供所需 photography 与已保存事实证据；递交手部来源特写本身不强制生成切角，同画面已能表达的内容可融入递交阶段。
相邻阶段若只是同一连续推进的切片，semantic_phase 必须相同；系统只在相同阶段名、连续承接和相同生成摄影方案下合并它们，并完整累计来源、事实和既有时长。不同目标推进不可为压阶段数而起相同名字；显式 cut、独立生成特写及揭示保持独立。
每个来源最多出现在一个生成阶段；阶段及阶段内 fact_ids 按来源时间顺序，省略来源不占动作时间。不要把阶段名当成新的动作事实，也不要靠名字省略事实。
每个新阶段填写 action_summary（单行、1–260字），把同一目标的连续子动作写成自然连贯的简洁生成编排，不机械按每个来源镜头抄一条句子，也不反复重述人物外貌和相同动作。递交→接稳、饮用→反馈→再次饮用的推进与重复必须清楚，必要信息特写/揭示也必须表达。
action_summary 是生成动作编排描述，不是新来源事实。causal_facts.text、fact_ids、来源归属和真实evidence仍保留完整原观察。摘要只概括本stage已有事实，不新增主体、道具、结果、因果，不把不确定动作改成确定行为。未分析原片音频，摘要不得断言交谈/聊天/说话/对白/台词；旧保存观察若写“交谈”，保留其原fact用于追溯，但生成摘要只写有视觉支持的非语言动作与反馈。
action_summary 只复述本stage.fact_ids引用的已校验fact.text中的可见动作，事件goal、其他stage、source_text或未经核实的emotion标签不能为摘要作证。品尝、吹气、喘息、疲惫/虚弱、疼痛/中毒/生病、心理感受和确定因果等重点推断词，若本stage.fact.text没有对应词不得写入；有对应词但其保存事实均含“或/可能/似乎/无法判断”，摘要也必须保留不确定性，或改写回已见嘴部/眼睛/手杯动作。
反例：“闭眼撅嘴后睁眼微笑”只能概括为闭眼、撅嘴、睁眼与微笑，不能新增“品尝”；“面红汗珠、嘴巴张合、眨眼”不能概括为“喘息、极度疲惫、中毒或生病”。“闭眼后吹气或品尝，具体无法判断”不能写成确定“品尝饮料”。“饮用”也不证明“喝光/喝完/喝尽/一饮而尽”，这些额外完成结果同样须有本stage.fact.text明确支持。普通饮用、微笑、目视、递杯可自然连贯表达，不按来源重复抄句。
选择 generation_subjects（最多4项）和 generation_objects（最多6项），精确复制本事件owned来源保存的 primary_subject/visual_facts.people 或 visual_facts.objects 标签；优先完整事件主要参与者、交接道具，不堆背景沙发/台灯/模糊路人，不为简洁改写标签或把未见名词加入列表。源人物/物件事实仍全部保留，不把后出现的人物塞进首帧。
阶段引用的 fact 必须完整覆盖该 fact 的全部来源；不得只引用一个来源却宣称完整的联合动作。
generation_mode=continuous 时阶段 transition 只能 initial/continuation；确有重要切换时用 internal_cuts 及 cut。
每个 event 自己的第一阶段必须 transition=initial，即使该事件与上一事件有原片切镜；只有同一事件内部后续阶段才允许 cut。
scene_continuity 与事件目标和摄影切角独立。描述本事件之前的相邻来源边界：unknown/same_scene/scene_change/time_elision。相同房间中开启新目标或普通切镜不是 scene_change；衣着、地点、昼夜或明显省略时间的新状态可支持真实时空变化，不能由喝水后躺卧推断中毒等未见因果。
非 unknown 的 scene_continuity 必须有 scene_evidence，真实引用本事件首来源和紧邻的前一来源两侧帧；首个全局来源没有前一来源，只能 unknown。已核实的 semantic scene/time_elision 边界不能被 same_scene 覆盖。
scene_evidence 直接复制 boundary_frame_catalog 中对应相邻边界的 recommended_evidence（frame_id 和原始 timestamp）。若两侧缺已送达帧，保持 unknown；不得从旧观察字符串、帧编号规律或猜测的末帧构造引用。
视觉保守/待语义复核 scene_group_id 和将“剧情时空或连续事件变化”混合标记的旧分组仅供参考，不是硬边界；根据实际帧和观察独立判断，不能每个新event都造一个新scene。
photography 是独立的生成摄影建议，给景别 shot_size、焦段 lens、唯一 camera_movement、构图 composition；未知焦段不得补造毫米数。
continuous 使用一套统一 photography；internal_cuts 可以在阶段 photography 中明确不同景别/构图，均为创作建议，不是原片实测。
窗口结束不代表事件结束。若 previous_tail_event 与本次首事件属于相同完整事件目标、已保存人物/道具与两侧状态支持连续，且未跨可信时空边界，可给 continuation_of_anchor_shot_id；不要求场景描述或状态自然语言逐字相等。
续接必须给 continuation_evidence，同时引用上个事件最后来源与本次首来源的真实帧。只能续接首事件，不得将只读来源重新列入 source_shot_ids。
每个阶段必须有 source_shot_ids、fact_ids、duration_seconds。以本事件可见来源真实总时长为预算，不因默认目标8秒而拉长；不要固定均分动作。系统只对最终生成总长按半秒向上进位，再安排阶段。总长超上限时在来源区间或有证据的语义阶段边界生成分片，原片时间不变。
首帧由系统使用第一段来源的初始状态独立渲染，禁止把未来动作塞入首帧。
仅两个模型调用：本次 initial，之后最多一次 refine 或 repair。可请求最多两次只读工具、最多六张新证据帧。
工具名仅 inspect_boundary/sample_interval_frames/compare_frames/crop_frame/get_neighbor_context；arguments 必须采用工具说明中字段。
拿到足够证据时 action=finalize 并给 events；缺少关键证据时 action=request_evidence 并给 tool_requests。
"""


def source_observation(shot, frame_ids=()):
    def delivered_refs(value):
        if isinstance(value, dict):
            if "frame_id" in value and (value["frame_id"] not in frame_ids
                    or value.get("crop_artifact_id") and value["crop_artifact_id"] not in frame_ids):
                return None
            return {key: delivered_refs(item) for key, item in value.items()}
        if isinstance(value, list):
            return [item for item in (delivered_refs(item) for item in value) if item is not None]
        return value

    details = shot.action_details.model_dump(mode="json")
    facts = shot.visual_facts.model_dump(mode="json")
    # Legacy string notes list every source frame, including unshown images.
    # Structured delivered evidence below is the only copyable citation source.
    details["evidence"], facts["evidence"] = [], []
    result = {
        "id": shot.id, "number": shot.number,
        "start_time": shot.start_time, "end_time": shot.end_time,
        "status": shot.status,
        "source_text": shot.source_text,
        "action": shot.action, "action_details": delivered_refs(details),
        "visual_facts": delivered_refs(facts),
        "shot_size": shot.shot_size, "lens": shot.lens,
        "camera_movement": shot.camera_movement, "composition": shot.composition,
        "scene_group_id": shot.scene_group_id, "scene_reason": shot.scene_reason, "cut_reason": shot.cut_reason,
        "analysis_warnings": shot.analysis_warnings,
    }
    result["saved_literal_observations"] = saved_observations(shot, set(frame_ids))
    return result


def build_window_prompt(window: EventWindow, shots, frames, config: EventAgentConfig, *,
                        phase="initial", previous_response="", errors=None, tool_results=None,
                        previous_tail_event=None, protected_source_shot_ids=None, protected_boundaries=None,
                        system_fallback_source_shot_ids=None, candidate_boundaries=None):
    scope = set(window.source_shot_ids + window.context_shot_ids)
    delivered_ids = {frame.id for frame in frames}
    source_frames = {sid: sorted((frame for frame in frames if frame.shot_id == sid),
                                key=lambda frame: (frame.timestamp, frame.id)) for sid in scope}
    boundaries = []
    ordered_sources = sorted(shots, key=lambda shot: (shot.start_time, shot.number, shot.id))
    for left, right in zip(ordered_sources, ordered_sources[1:]):
        if left.id not in scope or right.id not in window.source_shot_ids:
            continue
        left_frames, right_frames = source_frames[left.id], source_frames[right.id]
        boundaries.append({
            "left_source_shot_id": left.id, "right_source_shot_id": right.id,
            "left_delivered_frames": [{"frame_id": f.id, "timestamp": f.timestamp} for f in left_frames],
            "right_delivered_frames": [{"frame_id": f.id, "timestamp": f.timestamp} for f in right_frames],
            "recommended_evidence": [{"frame_id": f.id, "timestamp": f.timestamp, "description": "边界左侧" if index == 0 else "边界右侧"}
                                     for index, f in enumerate((left_frames[-1], right_frames[0]))]
                                    if left_frames and right_frames else [],
        })
    payload = {
        "window": window.model_dump(mode="json"),
        "owned_source_shot_ids": window.source_shot_ids,
        "context_shot_ids": window.context_shot_ids,
        "system_fallback_source_shot_ids": [sid for sid in window.context_shot_ids
                                           if sid in set(system_fallback_source_shot_ids or ())],
        "trusted_protected_source_shot_ids": [sid for sid in window.source_shot_ids
                                             if sid in set(protected_source_shot_ids or ())],
        "trusted_protected_boundaries": [{"left_shot_id": left, "right_shot_id": right}
                                         for left, right in sorted(protected_boundaries or ())
                                         if left in scope and right in scope],
        "candidate_boundaries": [row for row in (candidate_boundaries or [])
                                 if row.get("left_shot_id") in scope and row.get("right_shot_id") in scope],
        "source_observations": [source_observation(s, delivered_ids) for s in shots if s.id in scope],
        "boundary_frame_catalog": boundaries,
        "frames": [{"id": f.id, "shot_id": f.shot_id, "timestamp": f.timestamp,
                    "artifact_kind": f.artifact_kind, "parent_frame_id": f.parent_frame_id}
                   for f in frames],
        "budget": {"phase": phase, "generation_target_seconds": config.generation_target_seconds,
                   "generation_max_seconds": config.generation_max_seconds,
                   "model_calls_total": config.max_model_calls_per_window,
                   "tool_calls_total": config.max_tool_calls_per_window,
                   "extra_frames_total": config.max_extra_frames_per_window},
        "tools": {
            "inspect_boundary": {"timestamp": "数值，窗口内部的真实边界时间", "radius": "可选数值，0..1秒"},
            "sample_interval_frames": {"start_time": "数值", "end_time": "数值", "max_frames": "整数1..6"},
            "compare_frames": {"first_frame_id": "真实帧ID", "last_frame_id": "真实帧ID"},
            "crop_frame": {"frame_id": "真实帧ID", "bbox": [0.0, 0.0, 1.0, 1.0]},
            "get_neighbor_context": {},
        },
    }
    if previous_tail_event is not None:
        payload["previous_tail_event"] = {
            "id": previous_tail_event.id, "anchor_shot_id": previous_tail_event.anchor_shot_id,
            "goal": previous_tail_event.goal, "final_state": previous_tail_event.final_state,
            "last_source_shot_id": previous_tail_event.source_shot_ids[-1],
            "readonly": True,
        }
    if phase != "initial":
        payload["previous_response"] = previous_response
        payload["validation_errors"] = errors or []
        payload["tool_results"] = [r.model_dump(mode="json") for r in (tool_results or [])]
        payload["final_call"] = "这是最后一次模型调用。必须 finalize；不得再次请求工具。"
    return SYSTEM_RULES + "\n输入：\n" + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
