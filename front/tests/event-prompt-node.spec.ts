import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { Position } from '@vue-flow/core'
import EventPromptNode from '../src/components/EventPromptNode.vue'
import type { Prompt } from '../src/types'

const fullText = `完整事件：女人将杯子递给身旁的人。\n${'保留递杯、接杯及最终反应，镜头缓慢跟随手部动作。'.repeat(50)}\n结尾：接杯者确认并点头。`
const prompt: Prompt = {
  id: 'event-prompt-a', shotId: 'sequence-a', sequenceId: 'sequence-a', type: 'video',
  platform: 'seedance', version: 1, text: fullText, status: 'ready',
  templateId: 'event-sequence-v1', templateVersion: 1, warnings: [],
}

function card(lod?: 'overview' | 'strip' | 'detail', dimmed = false) {
  return mount(EventPromptNode, {
    props: { data: { prompt, sequenceNumber: 3, sourceCount: 5, title: '完整递杯事件', lod, dimmed } },
    global: { stubs: { Handle: true } },
  })
}

describe('complete event prompt node', () => {
  it.each([
    ['detail', '280px', '280px'],
    ['strip', '280px', '200px'],
    ['overview', '180px', '100px'],
  ] as const)('preserves every character in a fixed %s card', (lod, width, height) => {
    const wrapper = card(lod)
    const element = wrapper.get('[data-testid="event-prompt-card"]').element as HTMLElement
    expect(element.style.width).toBe(width)
    expect(element.style.height).toBe(height)
    expect(element.style.boxSizing).toBe('border-box')
    expect(wrapper.get('[data-testid="event-prompt-text"]').element.textContent).toBe(fullText)
    expect(wrapper.get('.event-prompt-body').classes()).toEqual(expect.arrayContaining(['nodrag', 'nowheel', 'nopan']))
    expect(wrapper.text()).toContain('完整事件 Prompt')
    expect(wrapper.text()).toContain('SEQ 03')
    expect(wrapper.text()).toContain('5 来源')
    expect(wrapper.text()).toContain('Seedance')
    expect(wrapper.text()).toContain('已完成')
  })

  it('defaults to detail and updates dimensions without discarding the full prompt', async () => {
    const wrapper = card()
    expect(wrapper.attributes('style')).toContain('height: 280px')
    await wrapper.setProps({ data: { prompt, sequenceNumber: 3, sourceCount: 5, lod: 'overview', dimmed: true } })
    expect(wrapper.attributes('style')).toContain('width: 180px')
    expect(wrapper.classes()).toContain('node-dimmed')
    expect(wrapper.get('[data-testid="event-prompt-text"]').element.textContent).toBe(fullText)
  })

  it('copies the full existing prompt without changing selection', async () => {
    const wrapper = card('overview')
    await wrapper.get('button[aria-label="复制完整事件 Prompt"]').trigger('click')
    expect(wrapper.emitted('copy')).toEqual([[fullText]])
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('renders and copies the positive and optional negative sections like the inspector', async () => {
    const wrapper = card()
    const combined = `${fullText}\n\nNegative Prompt:\n避免遮挡与额外切镜`
    await wrapper.setProps({ data: { prompt: { ...prompt, text: `  ${fullText}\n `, negativeText: '  避免遮挡与额外切镜  ' }, sequenceNumber: 3, sourceCount: 5 } })
    expect(wrapper.get('[data-testid="event-prompt-text"]').element.textContent).toBe(combined)
    await wrapper.get('button').trigger('click')
    expect(wrapper.emitted('copy')).toEqual([[combined]])
    expect(wrapper.emitted('select')).toBeUndefined()
  })

  it('ignores an empty negative section and never substitutes the canonical prompt ID', async () => {
    const wrapper = card()
    await wrapper.setProps({ data: { prompt: { ...prompt, negativeText: '   ' }, sequenceNumber: 3, sourceCount: 5 } })
    expect(wrapper.get('[data-testid="event-prompt-text"]').element.textContent).toBe(fullText)
    await wrapper.trigger('click')
    expect(wrapper.emitted('select')).toEqual([[prompt.id]])
  })

  it('selects by click or keyboard activation on the main card', async () => {
    const wrapper = card()
    await wrapper.trigger('click')
    await wrapper.trigger('keydown', { key: 'Enter' })
    await wrapper.trigger('keydown', { key: ' ' })
    expect(wrapper.emitted('select')).toEqual([[prompt.id], [prompt.id], [prompt.id]])
    expect(wrapper.attributes('role')).toBe('button')
    expect(wrapper.attributes('tabindex')).toBe('0')
  })

  it('lets undo and copy shortcuts bubble out of the read-only scroll body', () => {
    const wrapper = card()
    let bubbled = 0
    wrapper.element.addEventListener('keydown', () => { bubbled++ })
    const body = wrapper.get('.event-prompt-body').element
    body.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))
    expect(bubbled).toBe(0)
    for (const key of ['z', 'c']) body.dispatchEvent(new KeyboardEvent('keydown', { key, metaKey: true, bubbles: true }))
    expect(bubbled).toBe(2)
    expect(wrapper.emitted('select')).toBeUndefined()
    wrapper.unmount()
  })

  it('leaves scroll-body and copy-button keyboard events untouched', async () => {
    const wrapper = card()
    const body = wrapper.get('.event-prompt-body')
    const copy = wrapper.get('button')
    for (const target of [body, copy]) {
      for (const key of ['Enter', ' ']) {
        const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })
        target.element.dispatchEvent(event)
        expect(event.defaultPrevented).toBe(false)
      }
    }
    await wrapper.trigger('keydown', { key: 'Enter', ctrlKey: true })
    await wrapper.trigger('keydown', { key: ' ', repeat: true })
    expect(wrapper.emitted('select')).toBeUndefined()
    expect(wrapper.emitted('copy')).toBeUndefined()
  })

  it('exposes stable magnetic ports compatible with the video generator', () => {
    const handles = card().findAll('handle-stub')
    expect(handles).toHaveLength(2)
    expect(handles[0].attributes()).toMatchObject({ id: 'event-in', type: 'target', position: Position.Left })
    expect(handles[1].attributes()).toMatchObject({ id: 'prompt-out', type: 'source', position: Position.Right })
    expect(handles.every(handle => handle.classes().includes('magnetic-port'))).toBe(true)
  })
})
