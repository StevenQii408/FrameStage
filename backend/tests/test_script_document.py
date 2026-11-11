import pytest
from pydantic import ValidationError

from app.script_director.document import parse_script_document
from app.script_director.models import CONTENT_ROLES, ScriptDocument, ScriptSpan


RAIN_SCRIPT = (
    "【人物】\r\n"
    "程远（40岁，神色紧绷的刑警）\r\n"
    "老K（50多岁，瘸腿的地下线人）\r\n"
    "暴雨倾盆。程远拉低风衣帽檐，快步闪入天桥下的阴影处，雨水顺着他的衣角不断滴落。\r\n"
    "老K靠在斑驳的混凝土立柱旁，指间的香烟在黑暗中明灭不定。\r\n"
    "老K（吐出一口白雾，嗓音沙哑）：\r\n"
    "你迟到了五分钟。在咱们这行，五分钟足够死两次。\r\n"
    "程远（警惕地环视四周空旷的街道，压低声音）：\r\n"
    "路上有尾巴。东西带出来了吗？\r\n"
    "老K沉默片刻，从怀中摸出一个用防水胶带封死的微型硬盘。他停在半空，眼神复杂地盯着程远。\r\n"
    "老K：\r\n"
    "拿了这个，你就彻底回不去了。局里那帮人……比你想的还要脏。\r\n"
    "程远正欲接手，指尖刚触碰到冰冷的胶带——\r\n"
    "远处骤然响起刺耳的刹车声，两束惨白的车灯如利刃般撕裂雨幕，直挺挺照在两人身上！\r\n"
    "程远（瞳孔骤缩，迅速拔枪上膛）：\r\n"
    "走！\r\n"
    "老K却惨然一笑，猛地将硬盘塞进程远怀里，一把将他推向身后的排污暗渠。\r\n"
    "老K：\r\n"
    "他们是冲我来的。带着证据活下去！\r\n"
    "“砰——！”\r\n"
    "枪声伴随着滚滚惊雷骤然炸响。\n"
)


def test_latest_rain_script_separates_profiles_dialogue_and_sound():
    document = parse_script_document(RAIN_SCRIPT)
    assert [character.name for character in document.characters] == ["程远", "老K"]
    assert [character.description for character in document.characters] == [
        "40岁，神色紧绷的刑警", "50多岁，瘸腿的地下线人",
    ]
    dialogue = [span for span in document.spans if span.role == "dialogue"]
    assert [span.speaker for span in dialogue] == ["老K", "程远", "老K", "程远", "老K"]
    assert dialogue[0].text == "你迟到了五分钟。在咱们这行，五分钟足够死两次。"
    assert dialogue[3].parenthetical == "瞳孔骤缩，迅速拔枪上膛"
    by_id = {span.id: span for span in document.spans}
    assert any(by_id[ref].role == "parenthetical" for ref in dialogue[3].source_refs)
    assert [span.text for span in document.spans if span.role == "sound"] == [
        "“砰——！”", "枪声伴随着滚滚惊雷骤然炸响。",
    ]
    assert not any("40岁" in span.text or "50多岁" in span.text for span in document.content_spans)
    assert document.content_spans[0].text == "暴雨倾盆。"
    assert document.content_spans[0].role == "action"
    assert all(span.text == RAIN_SCRIPT[span.start:span.end] for span in document.spans)
    assert document.source_text == RAIN_SCRIPT
    assert parse_script_document(RAIN_SCRIPT).model_dump() == document.model_dump()


def test_metadata_and_profile_only_input_has_no_fabricated_content():
    document = parse_script_document("标题：雨夜\n【人物】\n程远（40岁，刑警）\n老K（50多岁，线人）")
    assert not document.content_spans
    assert not document.scenes
    assert [span.role for span in document.spans] == [
        "note", "note", "character_definition", "character_definition",
    ]


@pytest.mark.parametrize("label", ["画风", "整体画面风格", "视觉风格", "整体风格", "风格"])
@pytest.mark.parametrize("separator", ["：", ":"])
def test_style_instruction_is_a_traceable_note_not_a_character_or_dialogue(label, separator):
    source = f"  {label}{separator}多层剪纸动画，纸纤维边缘清晰。  \r\n男孩递出杯子。\r\n"
    document = parse_script_document(source)
    assert [(span.role, span.text) for span in document.spans] == [
        ("note", f"{label}{separator}多层剪纸动画，纸纤维边缘清晰。"),
        ("action", "男孩递出杯子。"),
    ]
    assert not document.characters
    assert [span.text for span in document.content_spans] == ["男孩递出杯子。"]
    assert all(span.text == source[span.start:span.end] for span in document.spans)
    assert document.spans[0].start == 2
    assert document.source_text == source


def test_bracketed_style_note_after_speaker_does_not_become_their_dialogue():
    source = "程远：\n【整体画面风格】：冷调新黑色电影。\n程远拔枪。"
    document = parse_script_document(source)
    assert [(span.role, span.text) for span in document.content_spans] == [("action", "程远拔枪。")]
    assert any(span.role == "note" and span.text == "【整体画面风格】：冷调新黑色电影。" for span in document.spans)
    assert [character.name for character in document.characters] == ["程远"]
    assert all(span.text == source[span.start:span.end] for span in document.spans)


@pytest.mark.parametrize(("heading", "body", "roles"), [
    ("内景 客厅 日。", "男孩递杯。女人接过杯子。", ["action", "action"]),
    ("1. EXT. COURTYARD - NIGHT.", "女孩：别走。", ["dialogue"]),
    ("场景：客厅 夜；", "音效：脚步声。", ["sound"]),
])
def test_inline_heading_keeps_the_same_content_as_a_separate_line(heading, body, roles):
    joined = "  " + heading + "  " + body
    doc = parse_script_document(joined)
    split = parse_script_document(heading + "\n" + body)
    assert [s.heading for s in doc.scenes] == [heading]
    assert [(s.role, s.text, s.speaker, s.scene_id) for s in doc.spans] == [
        (s.role, s.text, s.speaker, s.scene_id) for s in split.spans]
    assert [s.role for s in doc.content_spans] == roles
    assert all(joined[s.start:s.end] == s.text for s in doc.spans)


def test_ambiguous_title_and_ordinary_prose_keep_their_original_roles():
    source = "内景：放映厅《漫长的夜。第二章》 夜\n门口已经是深夜。男孩推开门。"
    doc = parse_script_document(source)
    assert [s.heading for s in doc.scenes] == ["内景：放映厅《漫长的夜。第二章》 夜"]
    assert [(s.role, s.text) for s in doc.content_spans] == [
        ("action", "门口已经是深夜。"), ("action", "男孩推开门。")]


def test_scene_ids_inline_dialogue_and_alias_identity_are_traceable():
    document = parse_script_document(
        "人物：\n老K/老王（50岁，瘸腿线人，化名老张）\n"
        "外景：天桥 夜\n老王：别回头。\n老张（压低声音）：走。\n"
        "内景：车库 夜\n老K：到了。"
    )
    character = document.characters[0]
    assert character.aliases == ["老王", "老张"]
    assert len(document.characters) == 1
    assert len(document.scenes) == 2
    dialogue = [span for span in document.spans if span.role == "dialogue"]
    assert [span.character_id for span in dialogue] == [character.id] * 3
    assert [span.scene_id for span in dialogue] == [document.scenes[0].id] * 2 + [document.scenes[1].id]
    assert all(scene.source_span_ids for scene in document.scenes)


def test_standalone_quotes_are_unknown_and_inner_punctuation_is_not_split():
    document = parse_script_document('“别走。等等！真的？”\n"为什么？"')
    assert [span.text for span in document.content_spans] == ['“别走。等等！真的？”', '"为什么？"']
    assert all(span.role == "dialogue" and span.speaker == "unknown" for span in document.content_spans)
    assert all(span.character_id is None for span in document.content_spans)
    assert not document.characters


def test_inline_quoted_speech_does_not_swallow_following_narration():
    document = parse_script_document('女孩（着急）："别走。等等！"。她推门出去。')
    assert [(span.role, span.text) for span in document.content_spans] == [
        ("dialogue", '"别走。等等！"。'), ("action", "她推门出去。"),
    ]
    assert document.content_spans[0].speaker == "女孩"
    assert document.content_spans[0].parenthetical == "着急"


def test_quotes_inside_prose_preserve_narration_and_bind_only_explicit_speech():
    document = parse_script_document('【人物】\n程远（40岁，刑警）\n程远说：“别走。等我！”他冲向门外。\n墙上写着“出口”。')
    dialogue = [span for span in document.spans if span.role == "dialogue"]
    assert [(span.text, span.speaker) for span in dialogue] == [('“别走。等我！”', "程远")]
    assert any(span.role == "action" and span.text == "他冲向门外。" for span in document.spans)
    assert any(span.role == "action" and span.text == "墙上写着“出口”。" for span in document.spans)


def test_voiceover_sound_notes_and_separate_parenthetical_are_preserved():
    document = parse_script_document(
        "备注：声音压过对白\n旁白：这座城从不睡觉。\n"
        "程远（VO）：我记得那个夜晚。\n程远：\n（压低声音）\n别动。\n音效：砰！"
    )
    assert [span.role for span in document.content_spans] == ["voiceover", "voiceover", "dialogue", "sound"]
    assert document.content_spans[2].parenthetical == "压低声音"
    assert document.content_spans[2].speaker == "程远"
    assert document.spans[0].role == "note"
    assert set(CONTENT_ROLES) == {"action", "dialogue", "voiceover", "sound"}


def test_person_name_at_start_of_action_does_not_inherit_pending_speaker():
    document = parse_script_document("人物：\n程远（40岁，刑警）\n程远：\n程远拔枪冲出去。")
    assert [span.role for span in document.content_spans] == ["action"]
    assert document.warnings


def test_auditory_only_prose_is_sound_and_does_not_swallow_following_action():
    document = parse_script_document("远处传来枪声。\n枪声响起。程远倒地。")
    assert [(span.role, span.text) for span in document.content_spans] == [
        ("sound", "远处传来枪声。"), ("sound", "枪声响起。"), ("action", "程远倒地。"),
    ]


def test_unknown_pronoun_speaker_is_not_registered_as_an_invented_person():
    document = parse_script_document('她说：“等一等。好吗？”\n某人：别过来。')
    dialogue = [span for span in document.spans if span.role == "dialogue"]
    assert [span.speaker for span in dialogue] == ["unknown", "unknown"]
    assert not document.characters


def test_empty_script_preserves_original_and_has_no_content():
    document = parse_script_document(" \r\n\t")
    assert document.source_text == " \r\n\t"
    assert not document.spans and not document.characters and not document.scenes


def test_document_rejects_untraceable_offsets_missing_source_and_unknown_refs():
    with pytest.raises(ValidationError, match="offsets"):
        ScriptSpan(id="span_1", start=0, end=9, text="hi", role="action")
    with pytest.raises(ValidationError, match="every non-whitespace"):
        ScriptDocument(source_text="missing")
    span = ScriptSpan(id="span_1", start=0, end=2, text="hi", role="action", source_refs=["bad"])
    with pytest.raises(ValidationError, match="unknown source references"):
        ScriptDocument(source_text="hi", spans=[span])


ROSTER = "【人物】\r\n程远（40岁，刑警）\r\n老K/老王（50岁，线人）\r\n"


@pytest.mark.parametrize("text,speaker", [
    ('程远看向老K，老K说：“快走。”', "老K"),
    ('程远看向老K，老王低声说：“快走。”', "老K"),
    ('“快走。”老K说。', "老K"),
    ('“快走。”程远对老K说。', "程远"),
    ('程远对老K说：“快走。”', "程远"),
    ('程远看向老K说：“快走。”', "unknown"),
])
def test_prose_speaker_uses_local_before_or_after_quote_grammar(text, speaker):
    source = ROSTER + text
    document = parse_script_document(source)
    dialogue = [span for span in document.content_spans if span.role == "dialogue"]
    assert len(dialogue) == 1 and dialogue[0].text == '“快走。”'
    assert dialogue[0].speaker == speaker
    assert [character.name for character in document.characters] == ["程远", "老K"]
    assert all(span.text == source[span.start:span.end] for span in document.spans)
    audit = document.attribution_audit[0]
    assert audit.span_id == dialogue[0].id
    assert audit.method == ("unknown" if speaker == "unknown" else "explicit")
    if speaker != "unknown":
        assert len(audit.source_span_ids) >= 2
        assert audit.character_id == next(character.id for character in document.characters if character.name == speaker)


def test_interrupted_quoted_dialogue_keeps_both_source_quotes_and_attribution():
    source = ROSTER + '“快走，”老K说，“别回头。”他转身。'
    document = parse_script_document(source)
    dialogue = [span for span in document.content_spans if span.role == "dialogue"]
    assert [(span.text, span.speaker) for span in dialogue] == [('“快走，”', "老K"), ('“别回头。”', "老K")]
    assert all(span.text == source[span.start:span.end] for span in document.spans)


@pytest.mark.parametrize("body", [
    '老K站在门口。他说：“快走。”',
    '老王站在门口。\r\n他说：“快走。”',
    '老K站在门口。“快走。”他说。',
    '老K站在门口。\r\n他：快走。',
])
def test_unambiguous_same_scene_pronoun_speaker_has_original_evidence(body):
    source = ROSTER + body
    document = parse_script_document(source)
    speaker = next(character for character in document.characters if character.name == "老K")
    dialogue = next(span for span in document.content_spans if span.role == "dialogue")
    assert dialogue.character_id == speaker.id and dialogue.speaker == "老K"
    audit = next(item for item in document.attribution_audit if item.span_id == dialogue.id)
    assert audit.method == "same-scene-context"
    by_id = {span.id: span for span in document.spans}
    assert any("站在门口" in by_id[sid].text for sid in audit.source_span_ids)
    assert any("他" in by_id[sid].text for sid in audit.source_span_ids)
    assert all(span.text == source[span.start:span.end] for span in document.spans)
    assert ScriptDocument.model_validate(document.model_dump()).model_dump() == document.model_dump()


@pytest.mark.parametrize("body", [
    '程远看向老K。他说：“快走。”',
    '老K站在门口。\n内景：车库 夜\n他说：“快走。”',
    '老K站在门口。雨继续下。他说：“快走。”',
    '老K站在门口。他们说：“快走。”',
    '他说：“快走。”',
])
def test_ambiguous_cross_scene_absent_or_plural_pronoun_stays_unknown(body):
    document = parse_script_document(ROSTER + body)
    dialogue = next(span for span in document.content_spans if span.role == "dialogue")
    assert dialogue.character_id is None and dialogue.speaker == "unknown"
    assert document.attribution_audit[0].method == "unknown"
    assert [character.name for character in document.characters] == ["程远", "老K"]


def test_pronoun_action_entity_reference_is_exact_and_preserves_antecedent():
    document = parse_script_document(ROSTER + "老K伸出手。他停在半空，盯着程远。")
    action = document.content_spans[-1]
    ref = next(ref for ref in action.entity_refs if ref.kind == "pronoun")
    assert ref.text == "他" and document.source_text[ref.start:ref.end] == "他"
    assert ref.character_id == next(character.id for character in document.characters if character.name == "老K")
    assert ref.source_span_ids == [document.content_spans[-2].id, action.id]
    broken = document.model_dump()
    broken["spans"][-1]["entity_refs"][0]["end"] += 1
    with pytest.raises(ValidationError, match="exact source tokens"):
        ScriptDocument.model_validate(broken)


def test_first_person_speech_uses_its_authored_speaker_and_collectives_remain_unresolved():
    document = parse_script_document(ROSTER + "程远和老K站在桥下。两人同时转头。\n旁白：我们都以为事情结束了。\n老K（VO）：我还记得那个夜晚。")
    references = [ref for span in document.spans for ref in span.entity_refs if ref.kind == "pronoun"]
    collective = [ref for ref in references if ref.text in ("两人", "我们")]
    assert len(collective) == 2 and all(ref.character_id is None for ref in collective)
    group = next(ref for ref in collective if ref.text == "两人")
    assert group.group_character_ids == [character.id for character in document.characters]
    assert next(ref for ref in collective if ref.text == "我们").method == "unknown"
    first_person = next(ref for ref in references if ref.text == "我")
    assert first_person.character_id == next(character.id for character in document.characters if character.name == "老K")
    assert first_person.method == "explicit"
    assert [character.name for character in document.characters] == ["程远", "老K"]


@pytest.mark.parametrize("prefix,expected_group_size", [
    ("程远和老K站在桥下。", 2),
    ("程远和老K站在桥下。\n内景：车库 夜\n", 0),
    ("程远站在桥下。", 0),
])
def test_first_person_plural_has_group_members_only_when_speaker_belongs_to_the_local_pair(prefix, expected_group_size):
    document = parse_script_document(ROSTER + prefix + "\n老K：我们一起离开。")
    ref = next(ref for span in document.content_spans for ref in span.entity_refs if ref.text == "我们")
    assert ref.character_id is None and len(ref.group_character_ids) == expected_group_size


def test_three_candidates_and_pronoun_characters_inside_names_cannot_become_a_pair():
    document = parse_script_document(ROSTER + "他尼（30岁，管理员）\n程远、老K和他尼站在桥下。两人转头。\n老K：我们一起离开。")
    refs = [ref for span in document.content_spans for ref in span.entity_refs]
    assert not any(ref.text == "他" and ref.kind == "pronoun" for ref in refs)
    assert all(not ref.group_character_ids and ref.character_id is None for ref in refs if ref.text in ("两人", "我们"))
