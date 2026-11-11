"""Version-preserving visual prompts, independent of inference SDKs."""
from typing import List, Optional

from app.config import Settings
from app.providers.base import FrameInput, ShotContext
from app.schemas import ScriptBeat


class VisionPromptBuilder:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _build_script_prompt(beats: List[ScriptBeat], target_beat_ids: List[str]) -> str:
        beat_text = "\n".join(f"{beat.id} | {beat.scene_hint} | {beat.scene_label or '未标注场景'} | {beat.paragraph_hint} | {beat.text}" for beat in beats)
        return f"""你是一名分镜导演。根据编号剧本节拍生成可拍摄分镜，只输出一个 JSON 对象，顶层字段为 shots。
只为 target_beat_ids={target_beat_ids} 生成结果；窗口内其他节拍仅作为连续性上下文。
每个节拍必须按原顺序恰好使用一次，只能合并相邻节拍；每镜 4.0、4.5 或 5.0 秒，action_steps 为 1–4 个具体可见动作。
action_steps 的每一项必须是完整的中文动作字符串，禁止填写序号、节拍编号或数字。例如："action_steps":["女孩伸手握住门把","女孩推门进入房间"]。
相邻节拍若主体、地点和动作连续，应合并成一镜；不要把每个节拍机械改写成单独镜头。强转折、主体变化或场景变化时保留边界。
不得编造剧本没有的角色、道具、事件或结果。首帧状态必须是动作发生前的可见状态。
每个 shot 必须包含：source_beat_ids、scene_group_id、primary_subject、first_frame_state、action_steps、interaction_target、end_state、emotion_change、shot_size、lens、camera_movement_type、camera_movement_detail、lighting_type、lighting_detail、composition、foreground、midground、background、spatial_relationships、continuity_anchors、duration_seconds、confidence、warnings。
枚举：shot_size=[大远景,远景,全景,中景,近景,特写,大特写,未知]；lens=[超广角,广角,标准,长焦,未知]；camera_movement_type=[固定,摇镜,俯仰,推近,拉远,跟拍,手持,升降,环绕,变焦,航拍,复合运动,未知]；lighting_type=[自然柔光,硬质方向光,高调,低调,逆光,实景光源,体积光,混合,未知]。
剧本节拍：
{beat_text}"""

    def _context_text(self, context: ShotContext) -> str:
        context_limit = min(max(self.settings.vlm_max_context_tokens, 256), 4096)
        script = context.script_text[: min(2400, max(100, context_limit // 2))]
        style = context.global_style[: min(600, max(50, context_limit // 10))]
        return f"当前镜头：{context.number:02d}，时间 {context.start_time:.2f}s - {context.end_time:.2f}s。\n相关剧本片段：{script or '无'}\n全局风格摘要：{style or '无'}"

    @staticmethod
    def _frame_sequence(frames: List[FrameInput], context: Optional[ShotContext] = None) -> str:
        labels = ["开始状态", "动作发展", "动作结果", "结束状态"] if len(frames) == 4 else ["开始状态", "中间状态", "结束状态", "补充状态"]
        duration = max((context.end_time - context.start_time) if context else 0.0, 0.001)
        parts = []
        for index, frame in enumerate(frames):
            relative = max(0, min(100, round((frame.timestamp - (context.start_time if context else frames[0].timestamp)) / duration * 100)))
            minutes = int(frame.timestamp // 60)
            seconds = frame.timestamp - minutes * 60
            parts.append(f"Frame {index + 1} | {minutes:02d}:{seconds:06.3f} | 镜头内 {relative}% | {labels[min(index, len(labels) - 1)]}")
        return "\n".join(parts)

    def _build_static_prompt(self, context: ShotContext, frames: List[FrameInput]) -> str:
        return f"""你是一名专业电影摄影指导。第一阶段只判断每张图中可直接观察的静态摄影信息，不分析运镜，不推测人物身份。
输出必须是唯一一个 JSON 对象，不要 Markdown 代码围栏，不要输出 JSON 之外的内容。
JSON 字段严格为：primary_subject、shot_size、lens、lighting_type、lighting_detail、lighting_vibe、composition、spatial_evidence、visual_style_evidence、visual_facts、confidence。
shot_size 只能是“大远景”“远景”“全景”“中景”“近景”“特写”“大特写”“未知”之一，表示主体在画面中的尺度；“超广角”绝对不是景别。
lens 是透视与视野的视觉焦段估计，只能是“超广角”“广角”“标准”“长焦”“未知”之一。
lighting_type 只能是“自然柔光”“硬质方向光”“高调”“低调”“逆光”“实景光源”“体积光”“混合”“未知”之一；lighting_detail 描述光源方向、颜色及画面证据。
primary_subject 在没有人物时也要描述主要可见主体，例如“日落下的城市与河流”，不能因为无人就写未知。
spatial_evidence 必须是对象，字段严格为 foreground、midground、background、relationships、observable。按前景遮挡、中景主体、背景环境和主体间位置关系记录可见证据；不可见就写未知，不得编造。
visual_facts 是对象：objects、people、scene、evidence、uncertainties；列出可见人物和物体、场景证据，无法核实的身份或用途放在 uncertainties。visual_style_evidence 是对象：color_palette、texture、visible_effects、evidence、uncertain_techniques。只记录可见色彩、质感和特效，每项证据引用 Frame 编号；无法核实的摄影技法放入 uncertain_techniques，不得当作事实。lighting_vibe 和 composition 使用简短、具体的中文描述。confidence 必须是 0 到 1 的数字。
输入代表帧（从本镜头四帧中按清晰度选择）：\n{self._frame_sequence(frames, context)}。
{self._context_text(context)}
"""

    def _build_motion_prompt(self, context: ShotContext, frames: List[FrameInput], retry: bool = False) -> str:
        retry_instruction = "上一次描述过于笼统。本次必须指出具体主体、动作方向、互动对象和末帧结果，并逐帧给出证据。" if retry else ""
        return f"""你是一名专业电影摄影指导。第二阶段只比较按时间排序的关键帧，判断画面在镜头内如何变化。
输出必须是唯一一个 JSON 对象，不要 Markdown 代码围栏，不要输出 JSON 之外的内容。
JSON 字段严格为：subject_action、primary_subject、initial_state、action_steps、interaction_target、final_state、emotion_change、visible_evidence、camera_movement_type、camera_movement_detail、confidence。
必须按“开始帧→中间帧→结束帧”比较主体位置、背景视差、画面覆盖范围和主体尺度变化。
camera_movement_type 只能是“固定”“摇镜”“俯仰”“推近”“拉远”“跟拍”“手持”“升降”“环绕”“变焦”“航拍”“复合运动”“未知”之一。
camera_movement_detail 要说明判断依据和方向，例如“航拍镜头向前移动并缓慢下降，城市建筑尺度逐渐增大”。
action_steps 是 1 到 3 条按发生顺序排列的具体动作；visible_evidence 使用“Frame N：可见变化”格式。subject_action 必须包含谁、做了什么以及对象或结果，不得只写“人物移动”“表情变化”“画面变化”。
subject_action 描述镜头中的主要视觉变化。没有人物时仍需描述场景或载具的变化，例如“无人机航拍城市并缓慢下降”，不得仅因为无人就填写未知。
只有各帧差异确实不足以判断时才可填写未知；confidence 必须是 0 到 1 的数字。
{retry_instruction}
帧序列：\n{self._frame_sequence(frames, context)}。
{self._context_text(context)}
"""
