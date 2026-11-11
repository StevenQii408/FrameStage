import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ShotNode from '../src/components/ShotNode.vue'
import { sourceRepresentativeFrame } from '../src/sourceRepresentativeFrame'
import { sequenceBranchTask } from './fixtures/sequenceBranches'

function observedSource() {
  const shot = sequenceBranchTask('evidence', 1).shots[0]
  shot.frames = [
    { id: 'frame-006-01', url: '/woman-residual.png', timestamp: 6.913333, width: 768, height: 1366 },
    { id: 'frame-006-02', url: '/boy-reaction.png', timestamp: 7.296667, width: 768, height: 1366 },
    { id: 'frame-006-03', url: '/boy-reaction-later.png', timestamp: 7.636667, width: 768, height: 1366 },
  ]
  shot.thumb = shot.frames[0].url
  shot.actionDetails.steps = ['保持微笑', '注视前方']
  shot.actionDetails.evidence = [
    'frame-006-03 | 7.637s：主体为卷发男孩，保持微笑',
    'frame-006-02 | 7.297s：主体为卷发男孩，注视前方',
  ]
  return shot
}

describe('source-card representative evidence frame', () => {
  it('labels script timing as a suggestion and does not offer a nonexistent frame asset', () => {
    const shot = { ...observedSource(), frames: [], thumb: '', script_direction: { schema_version: 'script-director.v3' } }
    const wrapper = mount(ShotNode, { props: { data: { shot, sourceEvidence: true } }, global: { stubs: { Handle: true }, directives: { motion: {} } } })
    expect(wrapper.text()).toContain('剧本分镜')
    expect(wrapper.text()).toContain('表演估计时长')
    expect(wrapper.text()).not.toContain('视觉置信度')
    expect(wrapper.find('handle-stub[id="frame-out"]').exists()).toBe(false)
    wrapper.unmount()
  })
  it('selects the earliest exactly owned, timestamp-checked legacy evidence without changing the first frame', () => {
    const shot = observedSource()
    const before = JSON.stringify(shot)
    expect(sourceRepresentativeFrame(shot)).toBe(shot.frames[1])
    expect(shot.frames[0].url).toBe('/woman-residual.png')
    expect(shot.thumb).toBe('/woman-residual.png')
    expect(JSON.stringify(shot)).toBe(before)
  })

  it('prefers structured action-step evidence and rejects unowned or inconsistent references', () => {
    const shot = observedSource()
    shot.actionDetails.stepEvidence = [[
      { frame_id: 'another-source-frame', timestamp: 7.297, description: '其他来源' },
      { frame_id: 'frame-006-02', timestamp: 6.913, description: '时间不一致' },
      { frame_id: 'frame-006-03', timestamp: 7.637, description: '已保存步骤的证据' },
    ]]
    expect(sourceRepresentativeFrame(shot)).toBe(shot.frames[2])
    shot.actionDetails.stepEvidence = []
    shot.actionDetails.evidence = ['another-source-frame | 7.297s：其他来源', 'frame-006-02 | 7.4s：错误时间', '未结构化的描述不能指定帧']
    expect(sourceRepresentativeFrame(shot)).toBeUndefined()
  })

  it('uses the evidence image only in source-card display and keeps its first-frame output handle', async () => {
    const shot = observedSource()
    const before = JSON.stringify(shot)
    const wrapper = mount(ShotNode, { props: { data: { shot, sourceEvidence: true } }, global: { stubs: { Handle: true }, directives: { motion: {} } } })
    await flushPromises()
    expect(wrapper.get('img').attributes('src')).toBe('/boy-reaction.png')
    expect(wrapper.get('.source-frame-evidence').text()).toContain('7.30s')
    expect(wrapper.find('handle-stub[id="frame-out"]').exists()).toBe(true)
    await wrapper.setProps({ data: { shot, sourceEvidence: false } })
    expect(wrapper.get('img').attributes('src')).toBe('/woman-residual.png')
    expect(JSON.stringify(shot)).toBe(before)
    wrapper.unmount()
  })
})
