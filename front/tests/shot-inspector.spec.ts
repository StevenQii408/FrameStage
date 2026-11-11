import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'
import ShotInspector from '../src/components/ShotInspector.vue'
import type { ActionPresentation, Prompt, Shot } from '../src/types'

function verticalShot(): Shot {
  return {
    id: 'shot-1', number: 1, time: '00.00 — 03.00', duration: '3.00s', action: '航拍城市并下降',
    shotSize: '大远景', lens: '超广角', movement: '航拍：向前并下降', lighting: '日落', confidence: 0.84,
    thumb: '/frame-a.jpg', status: 'ready', frames: [
      { id: 'a', url: '/frame-a.jpg', timestamp: 0.2, width: 768, height: 1366 },
      { id: 'b', url: '/frame-b.jpg', timestamp: 1.5, width: 768, height: 1366 },
      { id: 'c', url: '/frame-c.jpg', timestamp: 2.8, width: 768, height: 1366 },
    ], analysisWarnings: [], sourceRawShotIds: ['raw-1', 'raw-2'], sceneGroupId: 'scene-1',
    spatialEvidence: { foreground: '窗框', midground: '城市', background: '河流与落日', relationships: '城市位于河流左侧', observable: true },
    actionDetails: { primarySubject: '城市', initialState: '高空俯瞰', steps: ['镜头向前移动', '镜头缓慢下降'], interactionTarget: '城市天际线', finalState: '建筑尺度增大', emotionChange: '无', evidence: ['Frame 1→4'] },
  }
}

function actionPrompt(presentation: Partial<ActionPresentation> = {}, shotId = 'shot-1'): Prompt {
  return {
    id: `${shotId}-video`, shotId, type: 'video', platform: 'seedance', version: 1,
    text: '动作提示词', status: 'ready', templateId: 'seedance-video-v7', templateVersion: 7, warnings: [],
    contract: {
      purpose: '动作', firstFrameState: '女子手持杯子', primaryAction: '女子喝水', endState: '杯子离开面部',
      continuityAnchors: [], shotSize: '近景', lens: '标准', cameraMovement: '固定', lighting: '柔光', composition: '中心',
      delivery: { action_presentation: {
        schema_version: 'action-presentation.v1', main_action: '女子饮用杯中液体后放下杯子',
        accompanying_states: ['男孩抬头注视女子'], phases: [], timing_mode: 'none',
        source: 'model_summary', warnings: [], evidence_granularity: 'field', ...presentation,
      } },
    },
  }
}

function scriptShot(): Shot {
  return {
    ...verticalShot(), id: 'script-shot-1', frames: [], thumb: '',
    action: '阿青推开仓库门，听见门后的金属响声',
    script_direction: {
      schema_version: 'script-director.v3', event_id: 'event-1', title: '进入仓库',
      shot_function: '建立阿青的试探与门后的悬念', cut_reason: '转向声音来源以揭示反应', camera_angle: '平视偏侧',
      source_span_ids: ['span-2', 'span-3'],
      dialogue: [{ id: 'line-1', speaker_id: 'character-1', speaker_name: '阿青', text: '有人在里面吗？', parenthetical: '压低声音' }],
      sound_cues: ['门轴吱呀作响'], creative_assumptions: ['门缝中透出一线冷光'],
      character_profiles: [{ id: 'character-1', name: '阿青', description: '年轻修理工，袖口有机油痕迹', aliases: ['青子'] }],
      duration_kind: 'estimated-performance', continuity_notes: ['开门前后保持阿青右手位置一致'],
    },
  }
}

afterEach(() => { document.body.innerHTML = '' })

describe('ShotInspector', () => {
  it('shows script direction, attributed original dialogue and sound cues separately from creative assumptions', () => {
    const wrapper = mount(ShotInspector, { props: { shot: scriptShot() } })
    const direction = wrapper.get('.script-direction-section')
    expect(direction.text()).toContain('建立阿青的试探与门后的悬念')
    expect(direction.text()).toContain('转向声音来源以揭示反应')
    expect(direction.text()).toContain('平视偏侧')
    expect(direction.text()).toContain('span-2、span-3')
    expect(direction.text()).toContain('开门前后保持阿青右手位置一致')
    const dialogue = wrapper.get('.script-dialogue-section')
    expect(dialogue.text()).toContain('阿青')
    expect(dialogue.text()).toContain('压低声音')
    expect(dialogue.text()).toContain('有人在里面吗？')
    expect(dialogue.text()).toContain('门轴吱呀作响')
    expect(dialogue.text()).toContain('用于后续声音制作')
    expect(dialogue.text()).not.toContain('门缝中透出一线冷光')
    expect(wrapper.get('.script-assumptions-section').text()).toContain('门缝中透出一线冷光')
    expect(wrapper.get('.metric-grid').text()).toContain('表演估计时长')
    expect(wrapper.text()).not.toContain('VISIBLE FACTS')
    expect(wrapper.text()).not.toContain('OBSERVED STYLE')
    expect(wrapper.text()).not.toContain('详细动作观察')
    expect(wrapper.text()).not.toContain('在原视频中预览')
    expect(wrapper.text()).not.toContain('重新分析此镜头')
    wrapper.unmount()
  })

  it('keeps character profiles collapsed and displays aliases inside the inspector', async () => {
    const wrapper = mount(ShotInspector, { props: { shot: scriptShot() } })
    const profiles = wrapper.get('.script-character-profiles')
    expect(profiles.attributes('open')).toBeUndefined()
    expect(profiles.get('summary').text()).toContain('人物设定')
    ;(profiles.element as HTMLDetailsElement).open = true
    await profiles.trigger('toggle')
    expect(profiles.attributes('open')).toBe('')
    expect(profiles.text()).toContain('年轻修理工，袖口有机油痕迹')
    expect(profiles.text()).toContain('青子')
    await wrapper.setProps({ shot: { ...scriptShot(), id: 'script-shot-2' } })
    expect(wrapper.get('.script-character-profiles').attributes('open')).toBeUndefined()
    wrapper.unmount()
  })

  it('handles incomplete optional script metadata and missing speaker names', async () => {
    const wrapper = mount(ShotInspector, { props: { shot: { ...scriptShot(), script_direction: { schema_version: 'script-director.v3' } } } })
    expect(wrapper.get('.script-direction-section').text()).toContain('未记录')
    expect(wrapper.get('.script-dialogue-section').text()).toContain('未记录原文对白')
    expect(wrapper.get('.script-assumptions-section').text()).toContain('未记录创作补全')
    expect(wrapper.find('.script-character-profiles').exists()).toBe(false)
    await wrapper.setProps({ shot: { ...scriptShot(), script_direction: {
      ...scriptShot().script_direction!,
      dialogue: [
        { id: 'line-1', speaker_id: 'character-1', speaker_name: '', text: '还有谁？' },
        { id: 'line-2', speaker_id: null, speaker_name: '', text: '别开门。' },
      ],
    } } })
    const lines = wrapper.findAll('.script-dialogue-line')
    expect(lines[0].get('label').text()).toBe('阿青')
    expect(lines[1].get('label').text()).toBe('未标注说话人')
    wrapper.unmount()
  })

  it('restores the video inspector without retaining script metadata after changing shots', async () => {
    const wrapper = mount(ShotInspector, { props: { shot: scriptShot() } })
    await wrapper.setProps({ shot: verticalShot() })
    expect(wrapper.find('.script-direction-section').exists()).toBe(false)
    expect(wrapper.find('.script-dialogue-section').exists()).toBe(false)
    expect(wrapper.find('.script-assumptions-section').exists()).toBe(false)
    expect(wrapper.find('.script-character-profiles').exists()).toBe(false)
    expect(wrapper.get('.inspector-image img').attributes('src')).toBe('/frame-a.jpg')
    expect(wrapper.text()).toContain('空间证据')
    expect(wrapper.text()).toContain('在原视频中预览')
    expect(wrapper.get('.metric-grid').text()).toContain('置信度84%')
    expect(wrapper.text()).not.toContain('表演估计时长')
    expect(wrapper.text()).not.toContain('阿青')
    wrapper.unmount()
  })

  it('shows the evidence-bearing overall style without imposing a realistic default', () => {
    const shot = { ...verticalShot(), visualStyle: { medium: '3D动画', characterDesign: '大眼睛卷发男孩', rendering: '柔光三维渲染', colorPalette: '蓝白与暖米色', texture: '光滑表面', visibleEffects: [], evidence: ['frame-a | 0.200s：可见夸张头身比例'] } }
    const wrapper = mount(ShotInspector, { props: { shot } })
    expect(wrapper.text()).toContain('整体画面风格')
    expect(wrapper.text()).toContain('3D动画')
    expect(wrapper.text()).toContain('大眼睛卷发男孩')
    expect(wrapper.text()).toContain('frame-a | 0.200s')
    wrapper.unmount()
  })
  it('keeps a vertical frame ratio and switches among the three real frames', async () => {
    const wrapper = mount(ShotInspector, { props: { shot: verticalShot() } })
    expect(wrapper.find('.inspector-image').attributes('style')).toContain('aspect-ratio: 768 / 1366')
    expect(wrapper.find('.inspector-image img').attributes('src')).toBe('/frame-a.jpg')
    await wrapper.findAll('.frame-strip button')[1].trigger('click')
    expect(wrapper.find('.inspector-image img').attributes('src')).toBe('/frame-b.jpg')
    expect(wrapper.findAll('.frame-strip img').map((image) => image.attributes('src'))).toEqual(['/frame-a.jpg', '/frame-b.jpg', '/frame-c.jpg'])
    wrapper.unmount()
  })

  it('opens the lightbox and supports Escape, close button and backdrop closing', async () => {
    const wrapper = mount(ShotInspector, { props: { shot: verticalShot() } })
    await wrapper.get('[aria-label="放大关键帧"]').trigger('click')
    expect(document.querySelector('.shot-lightbox')).not.toBeNull()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })); await nextTick()
    expect(document.querySelector('.shot-lightbox')).toBeNull()

    await wrapper.get('[aria-label="放大关键帧"]').trigger('click')
    ;(document.querySelector('.lightbox-close') as HTMLElement).click(); await nextTick()
    expect(document.querySelector('.shot-lightbox')).toBeNull()

    await wrapper.get('[aria-label="放大关键帧"]').trigger('click')
    ;(document.querySelector('.shot-lightbox') as HTMLElement).click(); await nextTick()
    expect(document.querySelector('.shot-lightbox')).toBeNull()
    wrapper.unmount()
  })

  it('shows spatial/action evidence and traceable raw-cut reasons', async () => {
    const wrapper = mount(ShotInspector, { props: { shot: verticalShot(), rawShots: [
      { id: 'raw-1', number: 1, startTime: 0, endTime: 1.4, duration: 1.4, boundaryMetrics: { ssim: .92 }, decision: 'merge_next', reason: '视觉连续' },
      { id: 'raw-2', number: 2, startTime: 1.4, endTime: 3, duration: 1.6, boundaryMetrics: { protected: false }, decision: 'keep', reason: '视频结束' },
    ] } })
    expect(wrapper.text()).toContain('窗框')
    expect(wrapper.text()).toContain('镜头缓慢下降')
    await wrapper.setProps({ shot: { ...verticalShot(), cutReason: '动作停顿', sourceEditTimes: [1.4] } })
    expect(wrapper.text()).toContain('动作停顿')
    expect(wrapper.text()).toContain('1.40s')
    await wrapper.get('.raw-cuts summary').trigger('click')
    expect(wrapper.text()).toContain('视觉连续')
    wrapper.unmount()
  })

  it('shows a concise single action and accompaniment without manufacturing time segments', () => {
    const wrapper = mount(ShotInspector, { props: { shot: verticalShot(), prompts: [actionPrompt()] } })
    expect(wrapper.get('.inspector-summary').text()).toBe('女子饮用杯中液体后放下杯子')
    expect(wrapper.get('.action-accompaniment').text()).toContain('男孩抬头注视女子')
    expect(wrapper.find('.action-presentation-section').exists()).toBe(false)
    expect(wrapper.get('.action-evidence-section').attributes('open')).toBeUndefined()
    expect(wrapper.get('.action-evidence-section summary').text()).toContain('详细动作观察')
    expect(wrapper.text()).not.toContain('动作时间线')
    wrapper.unmount()
  })

  it('shows two meaningful timed phases as generation suggestions, not measured original timing', () => {
    const phases: ActionPresentation['phases'] = [
      { description: '拿起杯子并饮用', start_seconds: 0, end_seconds: 2.6, evidence: [], source_step_indices: [1, 2] },
      { description: '放下杯子并皱起眉头', start_seconds: 2.6, end_seconds: 4.8, evidence: [], source_step_indices: [3, 4] },
    ]
    const wrapper = mount(ShotInspector, { props: { shot: { ...verticalShot(), duration: '4.80s' }, prompts: [actionPrompt({ phases, timing_mode: 'generation-suggestion' })] } })
    const section = wrapper.get('.action-presentation-section')
    expect(section.text()).toContain('0–2.6 秒')
    expect(section.text()).toContain('2.6–4.8 秒')
    expect(section.text()).toContain('生成编排建议，不是原片动作时间实测')
    expect(section.findAll('li')).toHaveLength(2)
    wrapper.unmount()
  })

  it.each(['sequence', 'generation-suggestion'] as const)('does not claim precise times for %s when phase timing is unavailable or invalid', (timingMode) => {
    const phases: ActionPresentation['phases'] = [
      { description: '饮用液体', start_seconds: timingMode === 'sequence' ? null : 0, end_seconds: timingMode === 'sequence' ? null : 2, evidence: [], source_step_indices: [1] },
      { description: '摔碎杯子', start_seconds: timingMode === 'sequence' ? null : 1, end_seconds: timingMode === 'sequence' ? null : 6, evidence: [], source_step_indices: [2] },
    ]
    const wrapper = mount(ShotInspector, { props: { shot: verticalShot(), prompts: [actionPrompt({ phases, timing_mode: timingMode })] } })
    expect(wrapper.get('.action-presentation-section').text()).toContain('动作阶段')
    expect(wrapper.find('.action-phase-time').exists()).toBe(false)
    expect(wrapper.get('.action-presentation-section').text()).toContain('摔碎杯子')
    wrapper.unmount()
  })

  it('keeps frame timestamps in detailed evidence and falls back for old tasks', () => {
    const shot = { ...verticalShot(), actionDetails: { ...verticalShot().actionDetails, accompanyingStates: ['河面保持平静'], actionPhases: [
      { description: '缓慢下降', sourceStepIndices: [2], confidence: .8, evidence: [{ frame_id: 'b', timestamp: 1.5, description: '建筑尺度增大' }] },
    ] } }
    const wrapper = mount(ShotInspector, { props: { shot } })
    expect(wrapper.get('.inspector-summary').text()).toBe(shot.action)
    expect(wrapper.get('.action-evidence-section').text()).toContain('b · 1.5s：建筑尺度增大')
    expect(wrapper.get('.action-evidence-section').text()).toContain('不代表动作起止或完成时间')
    expect(wrapper.find('.action-presentation-section').exists()).toBe(false)
    wrapper.unmount()
  })

  it('replaces the presentation when selecting a different shot without retaining previous phase cards', async () => {
    const wrapper = mount(ShotInspector, { props: { shot: verticalShot(), prompts: [actionPrompt(), actionPrompt({ main_action: '男孩放下书本', accompanying_states: [] }, 'shot-2')] } })
    await wrapper.setProps({ shot: { ...verticalShot(), id: 'shot-2', action: '旧摘要' } })
    expect(wrapper.get('.inspector-summary').text()).toBe('男孩放下书本')
    expect(wrapper.find('.action-accompaniment').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('女子饮用杯中液体后放下杯子')
    expect(wrapper.get('.action-evidence-section').attributes('open')).toBeUndefined()
    wrapper.unmount()
  })
})
