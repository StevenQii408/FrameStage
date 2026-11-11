import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import GenerationSequenceNode from '../src/components/GenerationSequenceNode.vue'
import { sequenceCards } from '../src/generationSequences'
import { sequenceBranchTask } from './fixtures/sequenceBranches'

describe('sequence deck activation', () => {
  it('unfolds a merged deck without selecting it or opening an Inspector', async () => {
    const task = sequenceBranchTask('deck', 2)
    const wrapper = mount(GenerationSequenceNode, { props: { data: { shot: sequenceCards(task)[0], sources: task.shots } }, global: { stubs: { Handle: true } } })
    await wrapper.get('.sequence-deck-main').trigger('click')
    await wrapper.get('[data-testid="sequence-toggle"]').trigger('click')
    await wrapper.trigger('keydown', { key: 'Enter' })
    await wrapper.trigger('keydown', { key: ' ' })
    expect(wrapper.emitted('toggle')).toEqual(Array(4).fill([task.generationSequences![0].id]))
    expect(wrapper.emitted('select')).toBeUndefined()
    await wrapper.get('h3').trigger('click')
    expect(wrapper.emitted('select')).toEqual([[task.generationSequences![0].id]])
    expect(wrapper.emitted('toggle')).toHaveLength(4)
    wrapper.unmount()
  })

  it('keeps a single-source card selectable and has no redundant expansion control', async () => {
    const task = sequenceBranchTask('single', 1)
    const wrapper = mount(GenerationSequenceNode, { props: { data: { shot: sequenceCards(task)[0] } }, global: { stubs: { Handle: true } } })
    expect(wrapper.find('[data-testid="sequence-toggle"]').exists()).toBe(false)
    await wrapper.get('.sequence-deck-main').trigger('click')
    await wrapper.trigger('keydown', { key: ' ' })
    expect(wrapper.emitted('select')).toEqual(Array(2).fill([task.generationSequences![0].id]))
    expect(wrapper.emitted('toggle')).toBeUndefined()
    wrapper.unmount()
  })
})
