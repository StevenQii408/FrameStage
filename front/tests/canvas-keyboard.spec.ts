import { describe, expect, it } from 'vitest'
import { canvasShortcut, isCanvasUndoChord, isEditableTarget, isTextEditingTarget, isTrueCanvasModal } from '../src/canvasKeyboard'

describe('canvas keyboard shortcuts', () => {
  it('ignores held keys, handled events and redo combinations', () => {
    expect(canvasShortcut(new KeyboardEvent('keydown', { key: 'Backspace', repeat: true }), true)).toBeNull()
    expect(canvasShortcut(new KeyboardEvent('keydown', { key: 'Delete', isComposing: true }), true)).toBeNull()
    const handled = new KeyboardEvent('keydown', { key: 'Delete', cancelable: true })
    handled.preventDefault()
    expect(canvasShortcut(handled, true)).toBeNull()
    expect(canvasShortcut(new KeyboardEvent('keydown', { key: 'z', metaKey: true, shiftKey: true }), true)).toBeNull()
  })
  it('maps Command/Ctrl+Z globally and Delete/Backspace for a selected root or editable wire', () => {
    expect(canvasShortcut(new KeyboardEvent('keydown', { key: 'z', metaKey: true }), false)).toBe('undo')
    expect(canvasShortcut(new KeyboardEvent('keydown', { key: 'z', ctrlKey: true }), false)).toBe('undo')
    expect(canvasShortcut(new KeyboardEvent('keydown', { key: 'Delete' }), true)).toBe('delete')
    expect(canvasShortcut(new KeyboardEvent('keydown', { key: 'Backspace' }), true)).toBe('delete')
    expect(canvasShortcut(new KeyboardEvent('keydown', { key: 'Delete' }), false)).toBeNull()
    expect(isCanvasUndoChord(new KeyboardEvent('keydown', { key: 'Unidentified', code: 'KeyZ', metaKey: true }))).toBe(true)
    expect(isCanvasUndoChord(new KeyboardEvent('keydown', { key: 'z', metaKey: true, shiftKey: true }))).toBe(false)
  })

  it('does not steal editing shortcuts from inputs and textareas', () => {
    const input = document.createElement('input')
    const textarea = document.createElement('textarea')
    const backspace = new KeyboardEvent('keydown', { key: 'Backspace' })
    const undo = new KeyboardEvent('keydown', { key: 'z', metaKey: true })
    Object.defineProperty(backspace, 'target', { value: input })
    Object.defineProperty(undo, 'target', { value: textarea })
    expect(canvasShortcut(backspace, true)).toBeNull()
    expect(canvasShortcut(undo, true)).toBeNull()
  })

  it('routes readonly details Undo to canvas while keeping Delete protected', () => {
    const details = document.createElement('section')
    details.setAttribute('role', 'dialog'); details.setAttribute('aria-modal', 'false'); details.setAttribute('data-canvas-details', '')
    const viewer = document.createElement('textarea'); viewer.readOnly = true; details.append(viewer)
    const undo = new KeyboardEvent('keydown', { key: 'z', metaKey: true })
    const remove = new KeyboardEvent('keydown', { key: 'Delete' })
    Object.defineProperty(undo, 'target', { value: viewer }); Object.defineProperty(remove, 'target', { value: viewer })
    expect(isEditableTarget(viewer)).toBe(true)
    expect(isTextEditingTarget(viewer)).toBe(false)
    expect(isTrueCanvasModal(viewer)).toBe(false)
    expect(canvasShortcut(undo, true)).toBe('undo')
    expect(canvasShortcut(remove, true)).toBeNull()
    details.setAttribute('aria-modal', 'true')
    expect(canvasShortcut(undo, true)).toBeNull()
  })

  it('distinguishes enabled editors, disabled controls and inherited contenteditable=false', () => {
    const editor = document.createElement('textarea')
    expect(isTextEditingTarget(editor)).toBe(true)
    editor.disabled = true; expect(isTextEditingTarget(editor)).toBe(false)
    editor.disabled = false; editor.readOnly = true; expect(isTextEditingTarget(editor)).toBe(false)
    const parent = document.createElement('div'); parent.setAttribute('contenteditable', 'true')
    const child = document.createElement('span'); parent.append(child)
    expect(isTextEditingTarget(child)).toBe(true)
    child.setAttribute('contenteditable', 'false'); expect(isTextEditingTarget(child)).toBe(false)
  })

  it('accepts the legacy Safari keyCode fallback without capturing redo or tab commands', () => {
    const fallback = new KeyboardEvent('keydown', { key: 'Unidentified', metaKey: true })
    Object.defineProperty(fallback, 'keyCode', { value: 90 })
    expect(isCanvasUndoChord(fallback)).toBe(true)
    expect(isCanvasUndoChord(new KeyboardEvent('keydown', { key: 'z', metaKey: true, shiftKey: true }))).toBe(false)
    expect(isCanvasUndoChord(new KeyboardEvent('keydown', { key: 't', metaKey: true, shiftKey: true }))).toBe(false)
    expect(isCanvasUndoChord(new KeyboardEvent('keydown', { key: 'Process', code: 'KeyZ', metaKey: true, isComposing: true }))).toBe(false)
  })

  it('keeps atomic reference chips inside the Prompt editing keyboard context', () => {
    const editor = document.createElement('div')
    editor.setAttribute('data-prompt-editor', '')
    editor.setAttribute('contenteditable', 'true')
    const chip = document.createElement('span'); chip.setAttribute('contenteditable', 'false')
    const thumbnail = document.createElement('img'); chip.append(thumbnail); editor.append(chip)
    for (const target of [editor, chip, thumbnail]) {
      expect(isTextEditingTarget(target)).toBe(true)
      for (const props of [{ key: 'z', metaKey: true }, { key: 'Backspace' }]) {
        const event = new KeyboardEvent('keydown', props)
        Object.defineProperty(event, 'target', { value: target })
        expect(canvasShortcut(event, true)).toBeNull()
      }
    }
    editor.setAttribute('contenteditable', 'false')
    expect(isTextEditingTarget(thumbnail)).toBe(false)
  })
})
