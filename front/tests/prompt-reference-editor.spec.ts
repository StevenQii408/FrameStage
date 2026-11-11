import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PromptReferenceEditor from '../src/components/PromptReferenceEditor.vue'
import { atomicPromptSelection, getPromptSelection, readPromptEditorText, renderPromptEditor, setPromptSelection,
  type PromptEditorChange } from '../src/promptReferenceDom'
import { setPromptText } from './promptEditorTestHelpers'

const references = [
  { id: 'ref-a', mention: '图片1', url: '/portrait.jpg', selected: true },
  { id: 'ref-b', mention: '图片2', url: '/landscape.jpg', selected: true },
]
function create(value = '') {
  return mount(PromptReferenceEditor, { attachTo: document.body,
    props: { modelValue: value, references }, attrs: { 'aria-label': '生成 Prompt' }, global: { stubs: { Teleport: true } } })
}
function lastChange(wrapper: ReturnType<typeof create>) { return wrapper.emitted('change')?.at(-1)?.[0] as PromptEditorChange }

describe('plain-text reference editor', () => {
  it('renders noneditable chips while preserving an exact plain-text prompt and token identity', () => {
    const value = '参考 @图片1\n和 @图片2，生成画面。未知 @图片9 保留。'
    const wrapper = create(value)
    const editor = wrapper.get('[data-prompt-editor]')
    expect(editor.attributes()).toMatchObject({ contenteditable: 'true', role: 'textbox', 'aria-multiline': 'true' })
    expect(wrapper.findAll('[data-reference-token]')).toHaveLength(2)
    expect(wrapper.get('[data-reference-token="@图片1"]').attributes()).toMatchObject({ contenteditable: 'false', 'data-reference-mention': '图片1', draggable: 'false' })
    expect(wrapper.get('[data-reference-token="@图片1"] img').attributes('src')).toBe('/portrait.jpg')
    expect(wrapper.findAll('[data-reference-token][tabindex]')).toHaveLength(0)
    expect(readPromptEditorText(editor.element)).toBe(value)
    expect(wrapper.emitted('change')).toBeUndefined()
    wrapper.unmount()
  })

  it('maps the caret across mixed text and chips and removes a whole chip with one Backspace', async () => {
    const wrapper = create('前 @图片1 后')
    const editor = wrapper.get('[data-prompt-editor]')
    const root = editor.element as HTMLElement
    root.focus()
    setPromptSelection(root, 6)
    expect(getPromptSelection(root)).toEqual({ start: 6, end: 6 })
    await editor.trigger('keydown', { key: 'Backspace' })
    expect(lastChange(wrapper).value).toBe('前  后')
    expect(wrapper.find('[data-reference-token]').exists()).toBe(false)
    expect(references).toHaveLength(2)
    await editor.trigger('keydown', { key: 'z', metaKey: true })
    expect(lastChange(wrapper).value).toBe('前 @图片1 后')
    expect(wrapper.find('[data-reference-token]').exists()).toBe(true)
    await editor.trigger('keydown', { key: 'z', metaKey: true, shiftKey: true })
    expect(lastChange(wrapper).value).toBe('前  后')
    wrapper.unmount()
  })

  it('deletes a chip forward and never leaves a partial token from a selection', async () => {
    const wrapper = create('前 @图片1 后')
    const editor = wrapper.get('[data-prompt-editor]')
    ;(editor.element as HTMLElement).focus()
    setPromptSelection(editor.element as HTMLElement, 2)
    await editor.trigger('keydown', { key: 'Delete' })
    expect(lastChange(wrapper).value).toBe('前  后')
    expect(atomicPromptSelection('前 @图片1 后', { start: 3, end: 5 }, references)).toEqual({ start: 2, end: 6 })
    wrapper.unmount()
  })

  it('pastes only plain text, keeps line breaks, and never stores image URLs or HTML', async () => {
    const wrapper = create('')
    const editor = wrapper.get('[data-prompt-editor]')
    ;(editor.element as HTMLElement).focus()
    const event = new Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(event, 'clipboardData', { value: { getData: (type: string) => type === 'text/plain' ? '参考 @图片1\n<img onerror=alert(1)>' : '<img src="evil" onerror="alert(1)">' } })
    editor.element.dispatchEvent(event)
    await wrapper.vm.$nextTick()
    expect(event.defaultPrevented).toBe(true)
    expect(lastChange(wrapper).value).toBe('参考 @图片1\n<img onerror=alert(1)>')
    expect(wrapper.findAll('img')).toHaveLength(1)
    expect(wrapper.find('[onerror]').exists()).toBe(false)
    expect(lastChange(wrapper).value).not.toContain('/portrait.jpg')
    wrapper.unmount()
  })

  it('keeps IME composition untouched until confirmation and then materializes chips', async () => {
    const wrapper = create('')
    const editor = wrapper.get('[data-prompt-editor]')
    await editor.trigger('compositionstart')
    await setPromptText(editor, '中文 @图片1')
    expect(wrapper.emitted('change')).toBeUndefined()
    expect(wrapper.find('[data-reference-token]').exists()).toBe(false)
    await editor.trigger('keydown', { key: 'Enter', isComposing: true })
    expect(wrapper.emitted('change')).toBeUndefined()
    await editor.trigger('compositionend')
    expect(lastChange(wrapper).value).toBe('中文 @图片1')
    expect(wrapper.get('[data-reference-token]').text()).toBe('图片1')
    wrapper.unmount()
  })

  it('preserves newlines and handles Safari beforeinput undo/redo without a canvas action', async () => {
    const wrapper = create('首行')
    const editor = wrapper.get('[data-prompt-editor]')
    ;(editor.element as HTMLElement).focus()
    setPromptSelection(editor.element as HTMLElement, 2)
    await editor.trigger('keydown', { key: 'Enter' })
    expect(lastChange(wrapper).value).toBe('首行\n')
    editor.element.dispatchEvent(new InputEvent('beforeinput', { inputType: 'historyUndo', cancelable: true, bubbles: true }))
    expect(lastChange(wrapper).value).toBe('首行')
    editor.element.dispatchEvent(new InputEvent('beforeinput', { inputType: 'historyRedo', cancelable: true, bubbles: true }))
    expect(lastChange(wrapper).value).toBe('首行\n')
    wrapper.unmount()
  })

  it('restores external prompts and reference previews without turning missing references into another asset', async () => {
    const wrapper = create('使用 @图片1')
    await wrapper.setProps({ references: [references[1]] })
    expect(wrapper.find('[data-reference-token]').exists()).toBe(false)
    expect(readPromptEditorText(wrapper.get('[data-prompt-editor]').element)).toBe('使用 @图片1')
    await wrapper.setProps({ modelValue: '更换为 @图片2' })
    expect(wrapper.get('[data-reference-token]').attributes('data-reference-id')).toBe('ref-b')
    await wrapper.get('[data-reference-token] img').trigger('mouseover')
    expect(wrapper.get('[role="tooltip"] img').attributes('src')).toBe('/landscape.jpg')
    expect(wrapper.get('[role="tooltip"]').attributes('aria-label')).toBe('图片2完整预览')
    await wrapper.get('[data-prompt-editor]').trigger('mouseleave')
    expect(wrapper.find('[role="tooltip"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('enforces the same maximum length for typing and paste without leaking markup', async () => {
    const wrapper = create('')
    const editor = wrapper.get('[data-prompt-editor]')
    await setPromptText(editor, '测'.repeat(10010))
    expect(lastChange(wrapper).value).toHaveLength(10000)
    expect(readPromptEditorText(editor.element)).toHaveLength(10000)
    wrapper.unmount()
  })

  it('serializes native block wrappers and empty text without DOM-specific markup', () => {
    const root = document.createElement('div')
    root.append(document.createTextNode('第一行'))
    const line = document.createElement('div'); line.textContent = '第二行'; root.append(line)
    expect(readPromptEditorText(root)).toBe('第一行\n第二行')
    renderPromptEditor(root, '', references)
    expect(readPromptEditorText(root)).toBe('')
  })
})
