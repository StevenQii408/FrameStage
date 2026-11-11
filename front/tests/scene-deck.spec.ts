import { describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { makeSceneGroups } from '../src/graph'
import SceneNode from '../src/components/SceneNode.vue'
import type { SceneGroup, Shot } from '../src/types'

function shot(index: number): Shot {
  return {
    id: `shot-${index}`, number: index, time: `0 – ${index}`, duration: '4.5s', action: `动作${index}`,
    shotSize: '中景', lens: '标准', movement: '固定', lighting: '柔光', confidence: .8,
    thumb: `/frame-${index}.jpg`, frames: [{ id: `f-${index}`, url: `/frame-${index}.jpg`, timestamp: index, width: 768, height: 1366 }],
    analysisWarnings: [], status: 'ready', sourceRawShotIds: [], sceneGroupId: 'scene-001',
    actionDetails: { primarySubject: '人', initialState: '站立', steps: ['移动'], interactionTarget: '无', finalState: '停止', emotionChange: '无', evidence: [] },
    spatialEvidence: { foreground: '无', midground: '人', background: '室内', relationships: '居中', observable: true },
  }
}

describe('scene deck', () => {
  it('keeps eleven shots as one real scene without pagination', () => {
    const shots = Array.from({ length: 11 }, (_, index) => shot(index + 1))
    const group: SceneGroup = { id: 'scene-001', order: 1, title: '室内对话', shotIds: shots.map((item) => item.id), coverFrameIds: ['f-1'], startTime: 0, endTime: 49.5, warningCount: 0, groupingMode: 'semantic' }
    const scenes = makeSceneGroups(shots, [group])
    expect(scenes).toHaveLength(1)
    expect(scenes[0].shots).toHaveLength(11)
  })

  it('shows five floating depth cards and only toggles the whole scene', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }))
    const shots = Array.from({ length: 11 }, (_, index) => shot(index + 1))
    const wrapper = mount(SceneNode, {
      props: { data: { sceneId: 'task:scene:scene-001', index: 1, title: '室内对话', shots, timeRange: '0–49.5', warningCount: 0, progress: 100, expanded: true, lod: 'detail', groupingMode: 'semantic' } },
      global: { stubs: { Handle: true } },
    })
    expect(wrapper.findAll('.scene-deck-card')).toHaveLength(5)
    expect(wrapper.find('.scene-deck-card img').attributes('src')).toBe('/frame-1.jpg')
    await wrapper.find('.scene-deck-main').trigger('click')
    expect(wrapper.emitted('toggle')?.[0]).toEqual(['task:scene:scene-001'])
    expect(wrapper.find('.scene-deck-pager').exists()).toBe(false)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it('marks visual-only scene grouping as provisional', () => {
    const wrapper = mount(SceneNode, {
      props: { data: { sceneId: 'task:scene:scene-001', index: 1, title: '待复核场次', shots: [shot(1)], timeRange: '0–4.5', warningCount: 0, progress: 100, expanded: false, lod: 'detail', groupingMode: 'visual-fallback' } },
      global: { stubs: { Handle: true } },
    })
    expect(wrapper.text()).toContain('视觉分组 · 待复核')
    wrapper.unmount()
  })

  it('falls back to ten-shot presentation groups only without semantic identifiers', () => {
    const shots = Array.from({ length: 11 }, (_, index) => ({ ...shot(index + 1), sceneGroupId: '' }))
    const scenes = makeSceneGroups(shots)
    expect(scenes.map((scene) => scene.shots.length)).toEqual([10, 1])
    expect(scenes.every((scene) => scene.groupingMode === 'legacy-presentation-group')).toBe(true)
  })
  it('marks a paused visual grouping as unverified', () => {
    const wrapper = mount(SceneNode, {
      props: { data: { sceneId: 'task:scene:scene-001', index: 1, title: '未核验场次', shots: [shot(1)], timeRange: '0–4.5', warningCount: 0, progress: 30, expanded: false, lod: 'detail', groupingMode: 'visual-fallback', analysisPaused: true } },
      global: { stubs: { Handle: true } },
    })
    expect(wrapper.text()).toContain('分析暂停 · 场次未核验')
    wrapper.unmount()
  })
})
