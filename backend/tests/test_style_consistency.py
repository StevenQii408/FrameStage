"""Explicit-control safeguards only; no assertion of complete semantic entailment."""
import copy

from app.style_direct.consistency import check_consistency
from app.style_direct.models import InterpretationClaim, StyleProfileV3, render_profile
from test_style_direct import claim, evidence, interpretation, pipeline


def test_known_opposites_and_compressed_new_controls():
    assert not check_consistency('暖金高饱和', ['冷灰低饱和'], 'color_tone')['acceptable']
    assert check_consistency('冷灰低饱和', ['冷灰低饱和'], 'color_tone')['acceptable']
    assert not check_consistency('冷灰高饱和', ['冷灰'], 'color_tone', reject_new_controls=True)['acceptable']
    assert check_consistency('冷灰高饱和', ['冷灰'], 'color_tone')['acceptable']
    assert check_consistency('完全陌生的新词', ['冷灰低饱和'], 'color_tone')['coverage'] == 'explicit-controls-only'


def test_local_relationships_not_flattened_and_soft_volume_not_hard_shadow_conflict():
    source = '青蓝暗部，暖金高光'
    assert check_consistency(source, [source], 'color_tone', reject_new_controls=True)['acceptable']
    assert not check_consistency('整体暖金', [source], 'color_tone', reject_new_controls=True)['acceptable']
    assert not check_consistency('整体暖金', [source], 'color_tone')['acceptable']
    assert check_consistency('柔和明暗过渡', ['硬边阴影'], 'lighting_shading')['acceptable']
    assert not check_consistency('柔和阴影边缘', ['硬质阴影边界'], 'lighting_shading')['acceptable']
    assert check_consistency('并非高饱和，冷灰低饱和', ['冷灰低饱和'], 'color_tone', reject_new_controls=True)['acceptable']


def test_short_contradiction_locally_repaired_without_paid_retry(tmp_path):
    a, b = evidence(), interpretation()
    a['claims'].append(claim('C4', 'color_tone', '冷灰低饱和主调'))
    b['descriptions'].append(dict(dimension='color_tone', core_concept='冷灰色彩',
        professional_description='冷灰低饱和主调', concise_phrase='暖金高饱和', support_claim_ids=['C4']))
    p, provider = pipeline(tmp_path, [a, b])
    result = p.run()
    assert len(provider.calls) == 2 and not result['budget']['correction_used']
    assert '暖金' not in result['profile']['rendered_prompt']
    assert '冷灰低饱和' in result['profile']['rendered_prompt']
    assert '冷灰低饱和' in result['profile']['detailed_prompt']
    assert any(d.get('repair') == 'concise-contradiction-or-new-control-cleared' for d in result['audit'])
    assert result['profile']['provenance']['consistency_version'] == 'style-consistency.v1'


def test_wrong_long_description_restores_evidence_and_other_fields(tmp_path):
    a, b = evidence(), interpretation()
    a['claims'].append(claim('C4', 'color_tone', '冷灰低饱和主调'))
    b['descriptions'].append(dict(dimension='color_tone', core_concept='错误色彩',
        professional_description='暖金高饱和主调', concise_phrase='暖金高饱和', support_claim_ids=['C4']))
    p, provider = pipeline(tmp_path, [a, b])
    result = p.run()
    assert len(provider.calls) == 2
    assert result['interpretation']['descriptions'][-1]['professional_description'] == '冷灰低饱和主调'
    assert len(result['profile']['descriptions']) == 4
    assert any(d.get('repair') == 'description-contradiction-restored-from-evidence' for d in result['audit'])


def test_renderer_guards_profiles_built_outside_pipeline():
    item = InterpretationClaim(dimension='color_tone', core_concept='色彩',
        professional_description='冷灰低饱和主调', concise_phrase='暖金高饱和', support_claim_ids=['C1'])
    profile = StyleProfileV3(descriptions=[item])
    render_profile(profile)
    assert '暖金' not in profile.rendered_prompt
    assert '冷灰低饱和' in profile.rendered_prompt
