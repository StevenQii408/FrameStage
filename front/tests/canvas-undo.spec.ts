import { mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { canvasShortcut, focusCanvasOnPointer } from '../src/canvasKeyboard'
import { useCanvasUndo } from '../src/composables/useCanvasUndo'

let wrappers: VueWrapper[] = []
function host(undo = vi.fn(), options: { enabled?: () => boolean; hasHistory?: () => boolean; modalOpen?: () => boolean } = {}) {
  const view = mount(defineComponent({
    setup() {
      useCanvasUndo({ undo, ...options })
      return () => h('div', { tabindex: -1 }, [
        h('textarea', { readonly: true, 'data-viewer': '' }),
        h('textarea', { 'data-editor': '' }),
        h('section', { role: 'dialog', 'aria-modal': 'false', 'data-canvas-details': '' }, [h('textarea', { readonly: true })]),
        h('section', { role: 'dialog', 'aria-modal': 'true', 'data-canvas-modal': '' }, [h('textarea', { readonly: true })]),
        h('button', '工具'), h('div', { 'data-blank': '' }),
      ])
    },
  }), { attachTo: document.body })
  wrappers.push(view)
  return { view, undo }
}
function key(target: EventTarget, init: KeyboardEventInit = {}, type = 'keydown') {
  const event = new KeyboardEvent(type, { key: 'z', metaKey: true, bubbles: true, composed: true, cancelable: true, ...init })
  target.dispatchEvent(event)
  return event
}
function historyUndo(target: EventTarget, init: InputEventInit = {}) {
  const event = new InputEvent('beforeinput', { inputType: 'historyUndo', bubbles: true, composed: true, cancelable: true, ...init })
  target.dispatchEvent(event)
  return event
}

describe('canvas Undo event ownership', () => {
  beforeEach(() => { vi.useFakeTimers(); wrappers = [] })
  afterEach(() => { wrappers.forEach(view => view.unmount()); wrappers = []; vi.useRealTimers(); document.body.replaceChildren() })

  it('handles readonly Prompt Undo once before descendants can swallow it', () => {
    const { view, undo } = host()
    const viewer = view.get('[data-viewer]').element
    const local = vi.fn((event: Event) => event.stopPropagation())
    viewer.addEventListener('keydown', local)
    expect(key(viewer).defaultPrevented).toBe(true)
    expect(undo).toHaveBeenCalledTimes(1)
    expect(local).not.toHaveBeenCalled()
  })

  it('supports nonmodal readonly details but leaves true modal and text editing native', () => {
    const { view, undo } = host()
    expect(key(view.get('[data-canvas-details] textarea').element, { metaKey: false, ctrlKey: true }).defaultPrevented).toBe(true)
    expect(undo).toHaveBeenCalledTimes(1)
    const editor = view.get('[data-editor]').element
    expect(key(editor).defaultPrevented).toBe(false)
    expect(historyUndo(editor).defaultPrevented).toBe(false)
    expect(key(view.get('[data-canvas-modal] textarea').element).defaultPrevented).toBe(true)
    expect(historyUndo(view.get('[data-canvas-modal] textarea').element).defaultPrevented).toBe(true)
    expect(undo).toHaveBeenCalledTimes(1)
  })

  it('blocks browser UndoCloseTab in a readonly modal without undoing the underlying canvas', () => {
    let modalOpen = true
    const { view, undo } = host(vi.fn(), { modalOpen: () => modalOpen })
    expect(key(view.element).defaultPrevented).toBe(true)
    expect(historyUndo(view.element).defaultPrevented).toBe(true)
    expect(undo).not.toHaveBeenCalled()
    expect(key(view.get('[data-editor]').element).defaultPrevented).toBe(false)
    expect(historyUndo(view.get('[data-editor]').element).defaultPrevented).toBe(false)
    modalOpen = false
    expect(key(view.element).defaultPrevented).toBe(true)
    expect(undo).toHaveBeenCalledTimes(1)
  })

  it('deduplicates keydown plus beforeinput but allows later menu-delivered historyUndo', () => {
    const { view, undo } = host()
    key(view.element)
    expect(historyUndo(view.element).defaultPrevented).toBe(true)
    expect(undo).toHaveBeenCalledTimes(1)
    historyUndo(view.element)
    expect(undo).toHaveBeenCalledTimes(2)
    key(view.element)
    vi.advanceTimersByTime(181)
    historyUndo(view.element)
    expect(undo).toHaveBeenCalledTimes(4)
  })

  it('consumes empty-history Undo and held-key repeats without browser UndoCloseTab', () => {
    const { view, undo } = host(vi.fn(), { hasHistory: () => false })
    expect(key(view.element).defaultPrevented).toBe(true)
    expect(undo).toHaveBeenCalledTimes(1)
    expect(key(view.element, { repeat: true }).defaultPrevented).toBe(true)
    historyUndo(view.element)
    expect(undo).toHaveBeenCalledTimes(1)
  })

  it('does not intercept redo, CmdShiftT, keyup, IME or disabled canvas context', () => {
    let enabled = true
    const { view, undo } = host(vi.fn(), { enabled: () => enabled })
    for (const init of [{ shiftKey: true }, { key: 't', shiftKey: true }, { isComposing: true }, { altKey: true }]) {
      expect(key(view.element, init).defaultPrevented).toBe(false)
    }
    key(view.element, {}, 'keyup')
    enabled = false
    expect(key(view.element).defaultPrevented).toBe(false)
    expect(historyUndo(view.element).defaultPrevented).toBe(false)
    expect(undo).not.toHaveBeenCalled()
  })

  it('leaves noncancelable historyUndo, redo and composing beforeinput untouched', () => {
    const { view, undo } = host()
    historyUndo(view.element, { cancelable: false })
    historyUndo(view.element, { inputType: 'historyRedo' })
    historyUndo(view.element, { isComposing: true })
    expect(undo).not.toHaveBeenCalled()
  })

  it('unregisters both capture targets and pending timers when unmounted', () => {
    const { view, undo } = host()
    key(view.element)
    expect(undo).toHaveBeenCalledTimes(1)
    view.unmount(); wrappers = []
    expect(vi.getTimerCount()).toBe(0)
    key(document.body); historyUndo(document.body)
    expect(undo).toHaveBeenCalledTimes(1)
    const again = host()
    key(again.view.element)
    expect(again.undo).toHaveBeenCalledTimes(1)
    expect(undo).toHaveBeenCalledTimes(1)
  })

  it('uses the composed target so a shadow-root text editor keeps its native Undo', () => {
    const { view, undo } = host()
    const shadowHost = document.createElement('div'); view.element.append(shadowHost)
    const shadow = shadowHost.attachShadow({ mode: 'open' })
    const input = document.createElement('input'); shadow.append(input)
    expect(key(input).defaultPrevented).toBe(false)
    expect(undo).not.toHaveBeenCalled()
  })

  it('restores focus after readonly viewing, allowing Delete then canvas Undo', () => {
    const history: string[] = []; let deleted = false
    const { view, undo } = host(vi.fn(() => { if (history.pop()) deleted = false }))
    const root = view.element as HTMLElement
    root.addEventListener('pointerdown', event => focusCanvasOnPointer(event as PointerEvent, root), true)
    root.addEventListener('keydown', event => { if (canvasShortcut(event, true) === 'delete') { deleted = true; history.push('node') } })
    ;(view.get('[data-viewer]').element as HTMLTextAreaElement).focus()
    expect(document.activeElement).not.toBe(root)
    view.get('[data-blank]').element.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    expect(document.activeElement).toBe(root)
    key(root, { key: 'Delete', metaKey: false })
    expect(deleted).toBe(true)
    expect(key(root).defaultPrevented).toBe(true)
    expect(deleted).toBe(false)
    expect(undo).toHaveBeenCalledTimes(1)
  })

  it('does not steal focus from controls, ports, selectable text or scroll containers', () => {
    const { view } = host()
    const root = view.element as HTMLElement
    for (const target of [view.get('[data-viewer]').element, view.get('button').element]) {
      const event = new MouseEvent('pointerdown', { bubbles: true, button: 0 }); Object.defineProperty(event, 'target', { value: target })
      expect(focusCanvasOnPointer(event, root)).toBe(false)
    }
    for (const mode of ['handle', 'text', 'scroll']) {
      const target = document.createElement('div'); root.append(target)
      if (mode === 'handle') target.className = 'vue-flow__handle'
      if (mode === 'text') target.style.userSelect = 'text'
      if (mode === 'scroll') { target.style.overflowY = 'auto'; Object.defineProperty(target, 'scrollHeight', { value: 100 }); Object.defineProperty(target, 'clientHeight', { value: 10 }) }
      const event = new MouseEvent('pointerdown', { button: 0 }); Object.defineProperty(event, 'target', { value: target })
      expect(focusCanvasOnPointer(event, root)).toBe(false)
    }
  })
})
