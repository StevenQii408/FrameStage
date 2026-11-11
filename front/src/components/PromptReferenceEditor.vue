<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import ReferenceThumbnailPreview from './ReferenceThumbnailPreview.vue'
import { atomicPromptSelection, getPromptSelection, readPromptEditorText, renderPromptEditor, setPromptSelection,
  type PromptEditorChange, type PromptReference, type PromptSelection } from '../promptReferenceDom'

defineOptions({ inheritAttrs: false })
const props = withDefaults(defineProps<{ modelValue: string; references: PromptReference[]; placeholder?: string; maxlength?: number }>(), { maxlength: 10000, placeholder: '' })
const emit = defineEmits<{ change: [change: PromptEditorChange]; keydown: [event: KeyboardEvent]; composing: [value: boolean] }>()
const root = ref<HTMLElement>()
const composing = ref(false)
const preview = ref<{ url: string; label: string; anchor: { left: number; top: number; bottom: number } } | null>(null)
let value = props.modelValue
let remembered: PromptSelection = { start: value.length, end: value.length }
type Snapshot = { value: string; selection: PromptSelection }
// Keep editor undo independent from the canvas and Safari's browser-history command.
let history: Snapshot[] = [{ value, selection: { ...remembered } }]
let historyIndex = 0
let applyingHistory = false
function selection() { return root.value ? getPromptSelection(root.value, remembered) : remembered }
function notify() { emit('change', { value, ...remembered, composing: composing.value }) }
function draw(next: string, caret?: PromptSelection) {
  value = next
  if (!root.value) return
  const focused = document.activeElement === root.value
  renderPromptEditor(root.value, value, props.references)
  if (caret) { remembered = caret; if (focused) setPromptSelection(root.value, caret.start, caret.end) }
}
function record(next: string, caret: PromptSelection) {
  if (next !== value && !applyingHistory) {
    history[historyIndex].selection = { ...remembered }
    history = history.slice(0, historyIndex + 1)
    history.push({ value: next, selection: { ...caret } })
    if (history.length > 100) history.shift()
    historyIndex = history.length - 1
  }
  draw(next, caret)
  notify()
}
function input() {
  if (!root.value) return
  remembered = selection()
  const next = readPromptEditorText(root.value).slice(0, props.maxlength)
  if (composing.value) return
  record(next, { start: Math.min(remembered.start, next.length), end: Math.min(remembered.end, next.length) })
}
function updateSelection() { remembered = selection() }
function replaceRange(start: number, end: number, text: string) {
  const range = atomicPromptSelection(value, { start, end }, props.references)
  const replacement = text.replace(/\r\n?/g, '\n').slice(0, Math.max(0, props.maxlength - value.length + range.end - range.start))
  const next = value.slice(0, range.start) + replacement + value.slice(range.end)
  const cursor = range.start + replacement.length
  root.value?.focus()
  record(next, { start: cursor, end: cursor })
}
function undo(reverse = false) {
  updateSelection()
  history[historyIndex].selection = { ...remembered }
  const index = Math.max(0, Math.min(historyIndex + (reverse ? 1 : -1), history.length - 1))
  if (index === historyIndex) return
  historyIndex = index
  applyingHistory = true
  draw(history[index].value, history[index].selection)
  applyingHistory = false
  notify()
}
function keydown(event: KeyboardEvent) {
  emit('keydown', event)
  if (event.defaultPrevented || event.isComposing || event.keyCode === 229 || composing.value) return
  if ((event.metaKey || event.ctrlKey) && !event.altKey && (event.key.toLowerCase() === 'z' || event.key.toLowerCase() === 'y')) {
    event.preventDefault(); event.stopPropagation(); undo(event.shiftKey || event.key.toLowerCase() === 'y'); return
  }
  if (event.key === 'Enter') { event.preventDefault(); const range = selection(); replaceRange(range.start, range.end, '\n'); return }
  if (event.key !== 'Backspace' && event.key !== 'Delete') return
  const range = selection()
  const atomic = atomicPromptSelection(value, range, props.references, event.key === 'Backspace' ? 'backward' : 'forward')
  if (atomic.start !== range.start || atomic.end !== range.end) {
    event.preventDefault(); event.stopPropagation(); replaceRange(atomic.start, atomic.end, '')
  }
}
function beforeinput(event: InputEvent) {
  if (event.inputType === 'historyUndo' || event.inputType === 'historyRedo') { event.preventDefault(); undo(event.inputType === 'historyRedo') }
}
function paste(event: ClipboardEvent) {
  event.preventDefault()
  const range = selection()
  replaceRange(range.start, range.end, event.clipboardData?.getData('text/plain') || '')
}
function clipboard(event: ClipboardEvent, cut = false) {
  const range = atomicPromptSelection(value, selection(), props.references)
  if (range.start === range.end || !event.clipboardData) return
  event.preventDefault()
  event.clipboardData.setData('text/plain', value.slice(range.start, range.end))
  if (cut) replaceRange(range.start, range.end, '')
}
function endComposition() { composing.value = false; emit('composing', false); input() }
function pointerdown(event: PointerEvent) {
  const chip = (event.target as Element)?.closest<HTMLElement>('[data-reference-token]')
  if (!chip || !root.value || event.button !== 0) return
  event.preventDefault(); root.value.focus()
  const rect = chip.getBoundingClientRect()
  const range = document.createRange(); range.selectNodeContents(root.value)
  if (event.clientX < rect.left + rect.width / 2) range.setEndBefore(chip)
  else range.setEndAfter(chip)
  const end = readPromptEditorText(range.cloneContents()).length
  remembered = { start: end, end }; setPromptSelection(root.value, end)
}
function hover(event: MouseEvent) {
  const chip = (event.target as Element)?.closest<HTMLElement>('[data-reference-token]')
  const reference = props.references.find(item => item.id === chip?.dataset.referenceId)
  if (!chip || !reference?.url) { preview.value = null; return }
  const rect = chip.getBoundingClientRect()
  preview.value = { url: reference.url, label: reference.mention, anchor: { left: rect.left, top: rect.top, bottom: rect.bottom } }
}
watch(() => props.modelValue, next => {
  if (composing.value || next === value) return
  draw(next, { start: Math.min(remembered.start, next.length), end: Math.min(remembered.end, next.length) })
  history = [{ value: next, selection: { ...remembered } }]; historyIndex = 0
})
watch(() => props.references, () => { if (!composing.value) draw(value, selection()) }, { deep: true })
onMounted(() => draw(props.modelValue))
defineExpose({ focus: () => root.value?.focus(), getSelection: selection, setSelectionRange: (start: number, end = start) => {
  remembered = { start, end }; if (root.value) setPromptSelection(root.value, start, end)
}, replaceRange })
</script>

<template>
  <div ref="root" v-bind="$attrs" class="prompt-reference-editor" data-prompt-editor contenteditable="true" role="textbox" aria-multiline="true" spellcheck="false"
    :data-placeholder="placeholder" :aria-placeholder="placeholder || undefined" :data-empty="!modelValue" @input="input" @beforeinput="beforeinput" @keydown="keydown"
    @keyup="updateSelection" @mouseup="updateSelection" @blur="updateSelection" @pointerdown="pointerdown" @paste="paste" @copy="clipboard($event)" @cut="clipboard($event, true)"
    @compositionstart="composing = true; emit('composing', true)" @compositionend="endComposition" @mouseover="hover" @mouseleave="preview = null" />
  <ReferenceThumbnailPreview v-if="preview" v-bind="preview" @close="preview = null" />
</template>

<style scoped>
.prompt-reference-editor { overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; word-break: break-word; font-family: inherit; cursor: text; }
.prompt-reference-editor:focus { outline: none; }
.prompt-reference-editor:empty::before { content: attr(data-placeholder); color: #747b85; pointer-events: none; }
.prompt-reference-editor :deep(.prompt-reference-chip) { display: inline-flex; align-items: center; gap: 5px; box-sizing: border-box; max-width: 100%; height: 29px; margin: 0 3px; padding: 2px 7px 2px 2px; border: 1px solid #b4cbe02b; border-radius: 8px; background: #101720; color: #d9e7f1; vertical-align: middle; white-space: nowrap; user-select: all; cursor: default; }
.prompt-reference-editor :deep(.prompt-reference-chip img) { display: block; width: 23px; height: 23px; aspect-ratio: 1; object-fit: cover; border-radius: 5px; pointer-events: none; }
.prompt-reference-editor :deep(.prompt-reference-chip span) { font-size: .92em; pointer-events: none; }
.prompt-reference-editor :deep(.prompt-reference-chip:hover) { border-color: #8bbbd476; background: #1b2a36; }
.prompt-reference-editor :deep(.prompt-reference-chip.is-unselected) { border-style: dashed; opacity: .65; }
</style>
