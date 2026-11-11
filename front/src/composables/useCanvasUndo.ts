import { onBeforeUnmount, onMounted } from 'vue'
import { isCanvasUndoChord, isTextEditingTarget, isTrueCanvasModal, keyboardEventTarget } from '../canvasKeyboard'

interface CanvasUndoOptions {
  undo: () => void
  enabled?: () => boolean
  /** A modal pauses canvas undo, but readonly viewers must not leak it to Safari. */
  modalOpen?: () => boolean
  /** Empty history is still consumed and undo() may show its existing notice. */
  hasHistory?: () => boolean
}

/**
 * Capture real keyboard events before readonly viewers can swallow Cmd/Ctrl+Z.
 * beforeinput is a fallback only when the browser dispatches historyUndo;
 * browser chrome/menu commands that emit no page event cannot be overridden.
 */
export function useCanvasUndo(options: CanvasUndoOptions) {
  const handled = new WeakSet<Event>()
  let mounted = false
  let pendingKeyboardUndo = false
  let pendingTimer: ReturnType<typeof setTimeout> | undefined

  function clearPending() {
    pendingKeyboardUndo = false
    if (pendingTimer !== undefined) clearTimeout(pendingTimer)
    pendingTimer = undefined
  }
  function consume(event: Event) {
    handled.add(event)
    event.preventDefault()
    event.stopImmediatePropagation()
  }
  function handleKeydown(event: KeyboardEvent) {
    const target = keyboardEventTarget(event)
    if (handled.has(event) || options.enabled?.() === false || event.defaultPrevented
      || !isCanvasUndoChord(event) || isTextEditingTarget(target)) return
    consume(event)
    clearPending()
    if (options.modalOpen?.() || isTrueCanvasModal(target)) return
    pendingKeyboardUndo = true
    // A following native historyUndo belongs to this same keyboard action.
    pendingTimer = setTimeout(clearPending, 180)
    if (!event.repeat) options.undo()
  }
  function handleBeforeInput(event: InputEvent) {
    const target = keyboardEventTarget(event)
    if (handled.has(event) || options.enabled?.() === false || event.defaultPrevented || !event.cancelable
      || event.isComposing || event.inputType !== 'historyUndo' || isTextEditingTarget(target)) return
    consume(event)
    if (options.modalOpen?.() || isTrueCanvasModal(target)) { clearPending(); return }
    if (pendingKeyboardUndo) { clearPending(); return }
    options.undo()
  }
  onMounted(() => {
    mounted = true
    window.addEventListener('keydown', handleKeydown, true)
    document.addEventListener('keydown', handleKeydown, true)
    window.addEventListener('beforeinput', handleBeforeInput as EventListener, true)
    document.addEventListener('beforeinput', handleBeforeInput as EventListener, true)
  })
  onBeforeUnmount(() => {
    if (!mounted) return
    mounted = false
    clearPending()
    window.removeEventListener('keydown', handleKeydown, true)
    document.removeEventListener('keydown', handleKeydown, true)
    window.removeEventListener('beforeinput', handleBeforeInput as EventListener, true)
    document.removeEventListener('beforeinput', handleBeforeInput as EventListener, true)
  })
  return { handleKeydown, handleBeforeInput }
}
