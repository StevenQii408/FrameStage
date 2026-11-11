export type CanvasShortcut = 'undo' | 'delete' | null

function targetElement(target: EventTarget | null): Element | null {
  if (typeof Element === 'undefined') return null
  return target instanceof Element ? target : target instanceof Node ? target.parentElement : null
}

export function keyboardEventTarget(event: Event): EventTarget | null {
  return event.composedPath?.().find(item => typeof Element !== 'undefined' && item instanceof Element) || event.target
}

/** Conservative guard for Delete, Space and tool shortcuts, including viewers. */
export function isEditableTarget(target: EventTarget | null): boolean {
  const element = targetElement(target)
  return Boolean(element?.closest('input, textarea, select, button, a[href], form, [contenteditable]:not([contenteditable="false"]), [role="dialog"], [role="alertdialog"], [data-canvas-details], [data-canvas-modal]'))
}

/** A readonly Prompt viewer has no text-edit undo stack. */
export function isTextEditingTarget(target: EventTarget | null): boolean {
  const element = targetElement(target)
  if (!element) return false
  const control = element.closest('input, textarea') as HTMLInputElement | HTMLTextAreaElement | null
  if (control) {
    if (control.disabled || control.matches(':disabled') || control.readOnly) return false
    if (control.tagName === 'TEXTAREA') return true
    return !['button', 'submit', 'reset', 'checkbox', 'radio', 'range', 'color', 'file', 'image', 'hidden'].includes((control as HTMLInputElement).type)
  }
  // A reference chip is intentionally atomic/noneditable, but belongs to the
  // Prompt editor's text undo stack, never the canvas node undo/delete stack.
  if (element.closest('[data-prompt-editor][contenteditable="true"], [data-prompt-editor][contenteditable="plaintext-only"]')) return true
  for (let item: Element | null = element; item; item = item.parentElement) {
    const value = item.getAttribute('contenteditable')?.toLowerCase()
    if (value === 'false') return false
    if (value === '' || value === 'true' || value === 'plaintext-only') return true
  }
  return false
}

export function isTrueCanvasModal(target: EventTarget | null): boolean {
  return Boolean(targetElement(target)?.closest('[aria-modal="true"], [data-canvas-modal]:not([data-canvas-modal="false"])'))
}

export function isCanvasUndoChord(event: KeyboardEvent): boolean {
  const key = (event.key || '').toLowerCase()
  return (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && !event.isComposing && event.keyCode !== 229
    && (key === 'z' || (!key || key === 'unidentified') && (event.code === 'KeyZ' || event.keyCode === 90))
}

export function shouldHandleCanvasUndo(event: KeyboardEvent): boolean {
  const target = keyboardEventTarget(event)
  return !event.defaultPrevented && isCanvasUndoChord(event) && !isTextEditingTarget(target) && !isTrueCanvasModal(target)
}

export function canvasShortcut(event: KeyboardEvent, deletableSelected: boolean): CanvasShortcut {
  if (event.defaultPrevented || event.repeat || event.isComposing || event.keyCode === 229) return null
  if (shouldHandleCanvasUndo(event)) return 'undo'
  if (isEditableTarget(keyboardEventTarget(event))) return null
  if (deletableSelected && !event.metaKey && !event.ctrlKey && !event.altKey && (event.key === 'Backspace' || event.key === 'Delete')) return 'delete'
  return null
}

/** Restore canvas keyboard ownership without disturbing form or text selection. */
export function focusCanvasOnPointer(event: PointerEvent | MouseEvent, root: HTMLElement | null): boolean {
  const target = targetElement(keyboardEventTarget(event))
  if (!root || !target || !root.contains(target) || event.defaultPrevented || event.button !== 0) return false
  if (target.closest('input, textarea, select, button, a[href], summary, [role="button"], [role="link"], [role="slider"], [role="combobox"], [role="dialog"], [role="alertdialog"], [data-canvas-details], [data-canvas-modal], [data-canvas-no-focus], [data-canvas-selectable-text], .vue-flow__handle, .nowheel')) return false
  if (isTextEditingTarget(target)) return false
  for (let item: Element | null = target; item && item !== root; item = item.parentElement) {
    const style = window.getComputedStyle(item)
    if (style.userSelect === 'text' || style.getPropertyValue('-webkit-user-select') === 'text') return false
    const overflow = `${style.overflow} ${style.overflowX} ${style.overflowY}`
    if (/(auto|scroll)/.test(overflow) && (item.scrollHeight > item.clientHeight || item.scrollWidth > item.clientWidth)) return false
  }
  if (!root.hasAttribute('tabindex')) root.setAttribute('tabindex', '-1')
  root.focus({ preventScroll: true })
  return document.activeElement === root
}
