"""Conservative screenplay parsing without model calls or source normalization."""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from .models import (
    CONTENT_ROLES, ScriptAttributionAudit, ScriptCharacter, ScriptDocument,
    ScriptEntityReference, ScriptScene, ScriptSpan,
)


_CHARACTER_HEADER = re.compile(r"^[【\[（(\s]*(?:主要人物|人物介绍|人物设定|人物表|角色表|角色介绍|人物|角色)[】\]）)\s]*[：:]?\s*$")
_SCENE_HEADING = re.compile(
    r"^(?:(?:\d+[.、]\s*)?(?:INT\.?\s*[/／]\s*EXT\.?|INT\.|EXT\.)|"
    r"(?:\d+[.、]\s*)?(?:内景|外景)|第[一二三四五六七八九十百零\d]+场|"
    r"[【\[]?场景(?:[一二三四五六七八九十百零\d]+)?[】\]]?\s*[：:])", re.I,
)
# A pasted screenplay may join the slug line and action. Require an explicit
# time-of-day token and a sentence delimiter; punctuation in a title or INT.
# abbreviation is not a boundary.
_INLINE_SCENE_HEADING = re.compile(
    _SCENE_HEADING.pattern + r"(?:(?<=[：:])|(?=[ \t:：—–-]))"
    r"""[^。.!?！？；;“”「」《》"'（）()\r\n]*?[ \t/／·：:—–-]+"""
    r"(?:日|夜|昼|晨|昏|白天|夜晚|清晨|早晨|上午|中午|下午|傍晚|黄昏|深夜|凌晨|DAY|NIGHT|DAWN|DUSK)"
    r"[ \t]*(?:[。；;]|\.(?=[ \t]))(?=\s*\S)", re.I,
)
_NOTE = re.compile(
    r"^(?:[【\[]?(?:标题|片名|剧名|类型|题材|时长|简介|梗概|主题|整体画面风格|视觉风格|整体风格|画风|风格|备注|注|说明|"
    r"导演提示|镜头备注|人物关系|人物介绍|人物设定|人物表|角色表|角色介绍|主要人物|"
    r"人物|角色)[】\]]?\s*[：:]|【[^】]+】\s*$|\[[^\]]+\]\s*$)", re.I,
)
_NAME = r"[A-Za-z\u4e00-\u9fff][A-Za-z0-9_·.\u4e00-\u9fff /／、|]{0,23}"
_DEFINITION = re.compile(r"^(?P<name>" + _NAME + r")\s*[（(](?P<description>.+)[）)]\s*[。.;；]?$")
_DEFINITION_COLON = re.compile(r"^(?P<name>" + _NAME + r")\s*[：:]\s*(?P<description>.+)$")
_DESCRIPTION_CUE = re.compile(r"\d+\s*(?:多)?岁|年龄|性别|男性|女性|男[，、, ]|女[，、, ]|"
                              r"中年|青年|少年|少女|老人|外貌|身高|发型|长相|身穿|穿着|"
                              r"刑警|线人|警察|职业|又名|别名|化名|本名|昵称|又称")
_SPEAKER_CUE = re.compile(
    r"^(?P<name>" + _NAME + r")\s*(?P<performance>[（(][^\n]*?[）)])?\s*"
    r"(?P<mode>[（(](?:VO|V\.O\.|OS|O\.S\.|画外音|旁白)[）)])?\s*[：:]\s*", re.I,
)
_VOICEOVER_NAMES = {"旁白", "画外音", "独白", "VO", "V.O.", "OS", "O.S.", "NARRATOR"}
_UNKNOWN_NAMES = {"unknown", "未知", "未知人物", "某人", "她", "他", "它", "我", "我们", "他们", "她们", "它们", "有人"}
_SOUND_PREFIX = re.compile(r"^(?:音效|声音|声效|SFX|SOUND)\s*[：:]\s*", re.I)
_SOUND = re.compile(r"^(?:[“‘\"']?\s*(?:砰|嘭|啪|轰|咔嚓|哐|嘀|叮咚|Bang|BANG)[^\u4e00-\u9fffA-Za-z]*[”’\"']?|"
                    r"(?:枪声|枪响|爆炸声|雷声|惊雷|刹车声|铃声|脚步声|敲门声|尖叫声|鸣笛声|警笛声).*)[。！!？?]*$", re.I)
_SOUND_WORD = re.compile(r"枪声|枪响|爆炸声|雷声|惊雷|刹车声|铃声|脚步声|敲门声|尖叫声|鸣笛声|警笛声")
_VISIBLE_ACTION = re.compile(r"看|走|跑|拉|推|拿|伸|拔|塞|抱|抬|靠|滴落|站|坐|闪|转身|冲出|冲向|车灯|照在|撕裂|倒下|倒地")
_ALIAS = re.compile(r"(?:又名|别名|化名|本名|昵称|又称|人称|aka)\s*[：:]?\s*[“‘\"']?"
                    r"([A-Za-z\u4e00-\u9fff][A-Za-z0-9_·\u4e00-\u9fff]{0,15})", re.I)
_QUOTE_PAIRS = {"“": "”", "‘": "’", '"': '"'}
_TERMINATORS = frozenset("。！？!?；;")
_PRONOUN = re.compile(r"他们|她们|它们|我们|两人|二人|(?<!其)[他她它我](?!们)")
_SPEECH_VERB = r"(?:回答|开口|低语|嘀咕|说|问|喊|叫|答)[道着]?"
_SPEECH_ADVERB = r"(?:(?:轻声|低声|大声|高声|冷冷|缓缓|突然|小声|悄声|厉声|急忙|平静|沙哑|含糊|颤声|笑着|哭着|转头|抬头|压低声音)(?:地)?\s*){0,3}"


def _is_sound(text: str) -> bool:
    if _SOUND_PREFIX.match(text):
        return True
    if _VISIBLE_ACTION.search(text):
        return False
    return bool(_SOUND.fullmatch(text) or (_SOUND_WORD.search(text) and re.search(r"传来|响起|响彻|炸响|回荡|轰鸣", text)))


def _trim(source: str, start: int, end: int) -> Tuple[int, int]:
    while start < end and source[start].isspace():
        start += 1
    while end > start and source[end - 1].isspace():
        end -= 1
    return start, end


def _sentence_ranges(text: str) -> List[Tuple[int, int]]:
    """Split outside quotes/parentheses; closing quotes stay with their content."""
    ranges = []
    start = 0
    quote = None
    parens = []
    for index, char in enumerate(text):
        if quote:
            if char == quote and (index == 0 or text[index - 1] != "\\"):
                quote = None
            continue
        if char in _QUOTE_PAIRS:
            quote = _QUOTE_PAIRS[char]
            continue
        if char in "（(":
            parens.append(char)
        elif char in "）)" and parens:
            parens.pop()
        elif char in _TERMINATORS and not parens:
            ranges.append((start, index + 1))
            start = index + 1
    if start < len(text):
        ranges.append((start, len(text)))
    return ranges


def _quote_ranges(text: str) -> List[Tuple[int, int]]:
    ranges = []
    index = 0
    while index < len(text):
        closing = _QUOTE_PAIRS.get(text[index])
        if not closing:
            index += 1
            continue
        end = index + 1
        while end < len(text):
            if text[end] == closing and text[end - 1] != "\\":
                ranges.append((index, end + 1))
                index = end + 1
                break
            end += 1
        else:
            index += 1
    return ranges


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.spans: List[ScriptSpan] = []
        self.characters: List[ScriptCharacter] = []
        self.scenes: List[ScriptScene] = []
        self.by_name: Dict[str, ScriptCharacter] = {}
        self.scene: Optional[ScriptScene] = None
        self.in_roster = False
        self.pending: Optional[Tuple[Optional[ScriptCharacter], str, str, Optional[str], List[str]]] = None
        self.warnings: List[str] = []
        self.speech_cues: Dict[str, Tuple[str, int, int]] = {}
        self.attribution_audit: List[ScriptAttributionAudit] = []

    def character(self, raw_name: str, description: str = "") -> Optional[ScriptCharacter]:
        names = [part.strip() for part in re.split(r"[/／、|]", raw_name) if part.strip()]
        if not names or names[0] in _UNKNOWN_NAMES or names[0].upper() in _VOICEOVER_NAMES:
            return None
        # Conflicting aliases remain unresolved; never silently rebind identity.
        candidates = {self.by_name[name].id for name in names if name in self.by_name}
        if len(candidates) > 1:
            self.warnings.append("人物别名存在冲突：" + raw_name)
            return None
        character = next((self.by_name[name] for name in names if name in self.by_name), None)
        if character is None:
            character = ScriptCharacter(id="character_%04d" % (len(self.characters) + 1), name=names[0])
            self.characters.append(character)
            self.by_name[character.name] = character
        aliases = names[1:] + _ALIAS.findall(description)
        for alias in aliases:
            if alias == character.name or alias in character.aliases:
                continue
            if alias in self.by_name and self.by_name[alias].id != character.id:
                self.warnings.append("人物别名存在冲突：" + alias)
                continue
            character.aliases.append(alias)
            self.by_name[alias] = character
        if description and description not in character.description:
            character.description = "；".join(value for value in [character.description, description] if value)
        return character

    def ensure_scene(self) -> ScriptScene:
        if self.scene is None:
            self.scene = ScriptScene(id="scene_%04d" % (len(self.scenes) + 1))
            self.scenes.append(self.scene)
        return self.scene

    def add(self, start: int, end: int, role: str, character: Optional[ScriptCharacter] = None,
            speaker: Optional[str] = None, parenthetical: Optional[str] = None,
            refs: Optional[List[str]] = None) -> Optional[ScriptSpan]:
        start, end = _trim(self.text, start, end)
        if start == end:
            return None
        scene = self.ensure_scene() if role in CONTENT_ROLES or role == "parenthetical" else self.scene
        span_id = "span_%04d" % (len(self.spans) + 1)
        span = ScriptSpan(
            id=span_id, start=start, end=end, text=self.text[start:end], role=role,
            scene_id=scene.id if scene else None,
            character_id=character.id if character else None, speaker=speaker,
            parenthetical=parenthetical, source_refs=list(dict.fromkeys([*(refs or []), span_id])),
        )
        self.spans.append(span)
        return span

    def definition(self, line: str):
        match = _DEFINITION.fullmatch(line)
        if match and (self.in_roster or _DESCRIPTION_CUE.search(match.group("description"))):
            return match
        match = _DEFINITION_COLON.fullmatch(line)
        if self.in_roster and match and _DESCRIPTION_CUE.search(match.group("description")):
            return match
        return None

    def starts_action(self, line: str) -> bool:
        for name in sorted(self.by_name, key=len, reverse=True):
            if line.startswith(name) and len(line) > len(name):
                suffix = line[len(name):].lstrip()
                if suffix and suffix[0] not in "：:（(":
                    return True
        return bool(re.match(r"^(?:突然|这时|随后|接着|远处|与此同时|镜头|画面|内景|外景)", line))

    def speech_cue(self, context: str, after_quote=False):
        """Bind a local grammatical subject, never a name across another clause."""
        names = "|".join(re.escape(name) for name in sorted(self.by_name, key=len, reverse=True))
        subjects = "|".join(value for value in [names, "他们|她们|它们|他|她|它|某人|有人"] if value)
        addressed = r"(?:\s*(?:对|向)(?:" + names + r")\s*)?" if names else ""
        body = r"(?P<subject>" + subjects + r")\s*" + addressed + _SPEECH_ADVERB + _SPEECH_VERB
        if after_quote:
            return re.match(r"\s*[，,。！？!?；;]?\s*" + body + r"(?:\s*[，,。！？!?；;：:]|\s*$)", context)
        trimmed = context.rstrip(" \t：:，,")
        boundary = max((trimmed.rfind(char) for char in "，,。！？!?；;"), default=-1) + 1
        match = re.fullmatch(r"\s*(?:随后|接着|这时)?\s*" + body + r"\s*", trimmed[boundary:])
        if match:
            # Return source positions, including the discarded clause prefix.
            return match.group("subject"), boundary + match.start("subject"), boundary + match.end("subject")
        return None

    def dialogue_body(self, start: int, end: int, character: Optional[ScriptCharacter],
                      speaker: str, role: str, parenthetical: Optional[str], refs: List[str]):
        """Keep labelled unquoted speech intact; separate narration after a quote."""
        start, end = _trim(self.text, start, end)
        body = self.text[start:end]
        quotes = _quote_ranges(body)
        if not quotes or quotes[0][0] != 0:
            self.add(start, end, role, character, speaker, parenthetical, refs)
            return
        cursor = 0
        for left, right in quotes:
            if body[cursor:left].strip():
                break
            while right < len(body) and body[right] in _TERMINATORS:
                right += 1
            self.add(start + left, start + right, role, character, speaker, parenthetical, refs)
            cursor = right
        if cursor < len(body):
            self.prose(start + cursor, end)

    def prose(self, start: int, end: int):
        line = self.text[start:end]
        quotes = _quote_ranges(line)
        if not quotes:
            for left, right in _sentence_ranges(line):
                value = line[left:right].strip()
                self.add(start + left, start + right, "sound" if _is_sound(value) else "action")
            return
        cursor = 0
        for left, right in quotes:
            # Quoted prose (a sign, title, named object) is not speech without a cue.
            context = line[cursor:left]
            cue = self.speech_cue(context)
            cue_offset = cursor
            if cue is None:
                after = self.speech_cue(line[right:], after_quote=True)
                if after:
                    cue = after.group("subject"), after.start("subject"), after.end("subject")
                    cue_offset = right
            speaker = self.by_name.get(cue[0]) if cue else None
            pure_quote = not line[:left].strip() and not line[right:].strip(" \t。！？!?；;")
            spoken = cue is not None or pure_quote or bool(re.search(_SPEECH_VERB + r"[：:]\s*$", context))
            if not spoken:
                continue
            while right < len(line) and line[right] in _TERMINATORS:
                right += 1
            if left > cursor:
                for piece_start, piece_end in _sentence_ranges(line[cursor:left]):
                    self.add(start + cursor + piece_start, start + cursor + piece_end, "action")
            quoted = line[left:right]
            role = "sound" if _is_sound(quoted) else "dialogue"
            span = self.add(start + left, start + right, role, speaker,
                            speaker.name if speaker else ("unknown" if role == "dialogue" else None))
            if span and cue and role == "dialogue":
                self.speech_cues[span.id] = (cue[0], start + cue_offset + cue[1], start + cue_offset + cue[2])
            cursor = right
        if cursor < len(line):
            for left, right in _sentence_ranges(line[cursor:]):
                self.add(start + cursor + left, start + cursor + right, "action")

    def line(self, start: int, end: int):
        start, end = _trim(self.text, start, end)
        if start == end:
            self.pending = None
            return
        line = self.text[start:end]
        if _CHARACTER_HEADER.fullmatch(line):
            self.in_roster = True
            self.pending = None
            self.add(start, end, "note")
            return
        definition = self.definition(line)
        if definition:
            character = self.character(definition.group("name"), definition.group("description"))
            span = self.add(start, end, "character_definition", character)
            if character and span:
                character.source_span_ids.append(span.id)
            self.pending = None
            return
        self.in_roster = False
        if _SCENE_HEADING.match(line):
            self.pending = None
            self.scene = ScriptScene(id="scene_%04d" % (len(self.scenes) + 1), heading=line)
            self.scenes.append(self.scene)
            span = self.add(start, end, "scene_heading")
            self.scene.source_span_ids.append(span.id)
            return
        if _NOTE.match(line):
            self.pending = None
            self.add(start, end, "note")
            return
        if _is_sound(line):
            self.pending = None
            self.add(start, end, "sound")
            return
        cue = _SPEAKER_CUE.match(line)
        if cue:
            candidate_name = cue.group("name").strip()
            if candidate_name not in self.by_name and re.search(r"(?:说|问|喊|答|低语|嘀咕)[道着]?$", candidate_name):
                cue = None
        if cue:
            raw_name = cue.group("name").strip()
            performance = cue.group("performance")
            mode = cue.group("mode")
            is_voiceover = raw_name.upper() in _VOICEOVER_NAMES or bool(
                re.search(r"画外音|旁白|\bV\.?O\.?\b|\bO\.?S\.?\b", " ".join(value for value in [performance, mode] if value), re.I)
            )
            character = self.character(raw_name)
            speaker = character.name if character else (raw_name if is_voiceover else "unknown")
            refs = []
            cue_cursor = 0
            for group in ["performance", "mode"]:
                if cue.group(group):
                    left, right = cue.span(group)
                    span = self.add(start + cue_cursor, start + left, "note", character, speaker)
                    if span:
                        refs.append(span.id)
                    span = self.add(start + left, start + right, "parenthetical", character, speaker)
                    if span:
                        refs.append(span.id)
                    cue_cursor = right
            span = self.add(start + cue_cursor, start + cue.end(), "note", character, speaker)
            if span:
                refs.append(span.id)
            parenthetical = performance[1:-1].strip() if performance else None
            role = "voiceover" if is_voiceover else "dialogue"
            if cue.end() < len(line):
                self.dialogue_body(start + cue.end(), end, character, speaker, role, parenthetical, refs)
                self.pending = None
            else:
                self.pending = (character, speaker, role, parenthetical, refs)
            return
        if line.startswith(("（", "(")) and line.endswith(("）", ")")):
            character, speaker, _, _, refs = self.pending or (None, None, "dialogue", None, [])
            span = self.add(start, end, "parenthetical", character, speaker)
            if self.pending and span:
                self.pending = (character, speaker, self.pending[2], line[1:-1].strip(), [*refs, span.id])
            return
        if self.pending:
            pending = self.pending
            self.pending = None
            if not self.starts_action(line):
                character, speaker, role, parenthetical, refs = pending
                self.dialogue_body(start, end, character, speaker, role, parenthetical, refs)
                return
            self.warnings.append("人物提示后未找到明确台词：" + pending[1])
        self.prose(start, end)

    def _context_candidates(self, position: int, scene_id: str, exclude_span_id=None):
        """Only a local mention or the immediately previous content is evidence."""
        for span in reversed(self.spans):
            if span.start >= position or span.id == exclude_span_id or span.role not in CONTENT_ROLES:
                continue
            if span.scene_id != scene_id:
                return [], []
            refs = [ref for ref in span.entity_refs if ref.end <= position]
            ids = list(dict.fromkeys([*(ref.character_id for ref in refs if ref.character_id),
                *(cid for ref in refs for cid in ref.group_character_ids)]))
            unresolved = [ref for ref in refs if ref.kind == "pronoun" and not ref.character_id and not ref.group_character_ids]
            if span.end <= position and span.character_id:
                ids = list(dict.fromkeys([span.character_id, *ids]))
            if ids or unresolved or span.end <= position:
                candidates = list(dict.fromkeys([*ids, *(cid for ref in unresolved for cid in ref.candidate_character_ids)]))
                return ([] if unresolved else candidates), [span.id]
        return [], []

    def annotate_entities(self):
        """Resolve existing identities locally and retain every attribution decision."""
        names = sorted(self.by_name, key=len, reverse=True)
        tokens = []
        for name in names:
            token = re.escape(name)
            if name[0].isascii():
                token = r"(?<![A-Za-z0-9_])" + token
            if name[-1].isascii():
                token += r"(?![A-Za-z0-9_])"
            tokens.append(token)
        pattern = re.compile("|".join(tokens)) if tokens else None
        for span in self.spans:
            if span.role not in CONTENT_ROLES and span.role != "parenthetical":
                continue
            for match in pattern.finditer(span.text) if pattern else []:
                character = self.by_name[match.group()]
                span.entity_refs.append(ScriptEntityReference(start=span.start + match.start(),
                    end=span.start + match.end(), text=match.group(), kind="mention", character_id=character.id,
                    candidate_character_ids=[character.id], source_span_ids=[span.id], method="explicit"))
        for span in self.spans:
            if span.role not in CONTENT_ROLES:
                continue
            for match in _PRONOUN.finditer(span.text):
                position = span.start + match.start()
                if any(ref.kind == "mention" and ref.start <= position < ref.end for ref in span.entity_refs):
                    continue
                # Quoted dialogue pronouns describe speech content, not a
                # reliable new actor. They are recorded but remain unresolved.
                candidates, evidence = self._context_candidates(position, span.scene_id) if span.role == "action" else ([], [])
                singular = match.group() in ("他", "她", "它")
                cid = candidates[0] if singular and len(candidates) == 1 else None
                group = candidates if match.group() in ("两人", "二人") and len(candidates) == 2 else []
                span.entity_refs.append(ScriptEntityReference(start=position, end=span.start + match.end(),
                    text=match.group(), kind="pronoun", character_id=cid, group_character_ids=group,
                    candidate_character_ids=candidates,
                    source_span_ids=list(dict.fromkeys([*evidence, span.id])),
                    method="same-scene-context" if cid or group else "unknown"))
            span.entity_refs.sort(key=lambda ref: ref.start)
            if span.role not in ("dialogue", "voiceover"):
                continue
            cue = self.speech_cues.get(span.id)
            if cue is None and not span.character_id:
                for sid in span.source_refs:
                    label = next((item for item in self.spans if item.id == sid and item.role == "note"), None)
                    match = re.match(r"(?P<subject>他们|她们|它们|他|她|它)\s*[：:（(]", label.text) if label else None
                    if match:
                        cue = match.group("subject"), label.start + match.start("subject"), label.start + match.end("subject")
                        break
            method = "explicit" if span.character_id or span.role == "voiceover" else "unknown"
            evidence, candidates = list(span.source_refs), [span.character_id] if span.character_id else []
            if cue:
                subject, left, right = cue
                cue_span = next((item for item in self.spans if item.start <= left < right <= item.end), None)
                if cue_span:
                    evidence.append(cue_span.id)
                if subject in self.by_name:
                    character = self.by_name[subject]
                    span.character_id, span.speaker = character.id, character.name
                    candidates, method = [character.id], "explicit"
                elif subject in ("他", "她", "它"):
                    ref = next((item for item in cue_span.entity_refs if item.start == left and item.kind == "pronoun"), None) if cue_span else None
                    candidates, context_refs = (ref.candidate_character_ids, ref.source_span_ids) if ref else self._context_candidates(left, span.scene_id, span.id)
                    evidence.extend(context_refs)
                    if (ref is None or ref.character_id) and len(candidates) == 1:
                        character = next(item for item in self.characters if item.id == candidates[0])
                        span.character_id, span.speaker = character.id, character.name
                        method = "same-scene-context"
            span.source_refs = list(dict.fromkeys(evidence))
            # First-person speech names its own already attributed speaker.
            # Collective speech still has no unique identity to manufacture.
            if span.character_id:
                for ref in span.entity_refs:
                    if ref.kind == "pronoun" and ref.text == "我":
                        ref.character_id = span.character_id
                        ref.candidate_character_ids = [span.character_id]
                        ref.source_span_ids = list(span.source_refs)
                        ref.method = "explicit"
                    elif ref.kind == "pronoun" and ref.text == "我们":
                        group, group_refs = self._context_candidates(span.start, span.scene_id, span.id)
                        if len(group) == 2 and span.character_id in group:
                            ref.group_character_ids = group
                            ref.candidate_character_ids = group
                            ref.source_span_ids = list(dict.fromkeys([*group_refs, *span.source_refs]))
                            ref.method = "same-scene-context"
            self.attribution_audit.append(ScriptAttributionAudit(span_id=span.id,
                character_id=span.character_id, speaker=span.speaker or "unknown", method=method,
                source_span_ids=span.source_refs, candidate_character_ids=candidates,
                reason="原文局部结构明确说话人" if method == "explicit" else
                    "同场景紧邻原文只有一个已有角色候选" if method == "same-scene-context" else
                    "原文未提供唯一说话人证据，保留未知归属"))

    def parse(self) -> ScriptDocument:
        offset = 0
        for raw_line in self.text.splitlines(keepends=True):
            start, end = _trim(self.text, offset, offset + len(raw_line))
            while heading := _INLINE_SCENE_HEADING.match(self.text[start:end]):
                boundary = start + heading.end()
                self.line(start, boundary)
                start, end = _trim(self.text, boundary, end)
            self.line(start, end)
            offset += len(raw_line)
        if self.pending:
            self.warnings.append("人物提示后缺少台词：" + self.pending[1])
        self.annotate_entities()
        return ScriptDocument(source_text=self.text, spans=self.spans, characters=self.characters,
                              scenes=self.scenes, warnings=list(dict.fromkeys(self.warnings)),
                              attribution_audit=self.attribution_audit)


def parse_script_document(text: str) -> ScriptDocument:
    """Parse screenplay/prose while preserving exact source slices and identity.

    Unknown quoted speakers remain ``unknown``. Unlabelled prose is action;
    character definitions and production notes never become event coverage.
    """
    return _Parser(text).parse()
