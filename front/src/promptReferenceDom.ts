/** The editor is visual only: persisted prompts always remain plain @图片N text. */
export interface PromptReference { id: string; mention: string; url: string; selected?: boolean }
export interface PromptSelection { start: number; end: number }
export interface PromptEditorChange extends PromptSelection { value: string; composing: boolean }

const blockTags = new Set(['DIV', 'P', 'LI'])
export function readPromptEditorText(root: Node): string {
  function read(node: Node): string {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent || ''
    if (!(node instanceof Element)) return Array.from(node.childNodes).map(read).join('')
    if (node.hasAttribute('data-reference-token')) return node.getAttribute('data-reference-token') || ''
    if (node.tagName === 'BR') return node.hasAttribute('data-editor-placeholder') ? '' : '\n'
    let value = ''
    Array.from(node.childNodes).forEach((child, index) => {
      if (child instanceof Element && blockTags.has(child.tagName) && index > 0 && !value.endsWith('\n')) value += '\n'
      value += read(child)
    })
    return value
  }
  return read(root).replace(/\r\n?/g, '\n')
}

export function getPromptSelection(root: HTMLElement, fallback?: PromptSelection): PromptSelection {
  const selection = root.ownerDocument.getSelection()
  if (!selection?.rangeCount || !root.contains(selection.anchorNode) || !root.contains(selection.focusNode)) {
    return fallback || { start: readPromptEditorText(root).length, end: readPromptEditorText(root).length }
  }
  const selected = selection.getRangeAt(0)
  const prefix = root.ownerDocument.createRange()
  prefix.selectNodeContents(root)
  prefix.setEnd(selected.startContainer, selected.startOffset)
  const start = readPromptEditorText(prefix.cloneContents()).length
  prefix.setEnd(selected.endContainer, selected.endOffset)
  return { start, end: readPromptEditorText(prefix.cloneContents()).length }
}

export function setPromptSelection(root: HTMLElement, start: number, end = start) {
  const length = readPromptEditorText(root).length
  const bounds = [Math.max(0, Math.min(start, length)), Math.max(0, Math.min(end, length))]
  function point(offset: number): [Node, number] {
    let remaining = offset
    for (const child of Array.from(root.childNodes)) {
      const count = readPromptEditorText(child).length
      if (child.nodeType === Node.TEXT_NODE && remaining <= count) return [child, remaining]
      if (child instanceof Element && child.hasAttribute('data-reference-token') && remaining < count) {
        const index = Array.prototype.indexOf.call(root.childNodes, child)
        return [root, index + (remaining > count / 2 ? 1 : 0)]
      }
      if (remaining === 0) return [root, Array.prototype.indexOf.call(root.childNodes, child)]
      remaining -= count
    }
    return [root, root.childNodes.length]
  }
  const range = root.ownerDocument.createRange()
  range.setStart(...point(bounds[0]))
  range.setEnd(...point(bounds[1]))
  const selection = root.ownerDocument.getSelection()
  selection?.removeAllRanges()
  selection?.addRange(range)
}

export function renderPromptEditor(root: HTMLElement, value: string, references: PromptReference[]) {
  const byMention = new Map(references.map(reference => ['@' + reference.mention, reference]))
  const nodes: Node[] = []
  let cursor = 0
  for (const match of value.matchAll(/@图片[1-9]\d*/g)) {
    const reference = byMention.get(match[0])
    if (!reference?.url) continue
    nodes.push(document.createTextNode(value.slice(cursor, match.index)))
    const chip = document.createElement('span')
    chip.className = 'prompt-reference-chip' + (reference.selected === false ? ' is-unselected' : '')
    chip.setAttribute('contenteditable', 'false')
    chip.setAttribute('data-reference-token', match[0])
    chip.setAttribute('data-reference-mention', reference.mention)
    chip.setAttribute('data-reference-id', reference.id)
    chip.setAttribute('aria-label', match[0])
    chip.setAttribute('draggable', 'false')
    const image = document.createElement('img')
    image.src = reference.url
    image.alt = ''
    image.loading = 'lazy'
    image.draggable = false
    const label = document.createElement('span')
    label.textContent = reference.mention
    chip.append(image, label)
    nodes.push(chip)
    cursor = match.index! + match[0].length
  }
  nodes.push(document.createTextNode(value.slice(cursor)))
  root.replaceChildren(...nodes)
}

/** Normalize partial selections so a reference is always edited as one token. */
export function atomicPromptSelection(value: string, selection: PromptSelection, references: PromptReference[], direction?: 'backward' | 'forward'): PromptSelection {
  let { start, end } = selection
  const known = new Set(references.filter(reference => reference.url).map(reference => '@' + reference.mention))
  for (const match of value.matchAll(/@图片[1-9]\d*/g)) {
    if (!known.has(match[0])) continue
    const a = match.index!, b = a + match[0].length
    if (start === end && (direction === 'backward' && start === b || direction === 'forward' && start === a)) return { start: a, end: b }
    if (start < b && end > a) { start = Math.min(start, a); end = Math.max(end, b) }
  }
  return { start, end }
}
