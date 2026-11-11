import type { DOMWrapper } from '@vue/test-utils'
import { setPromptSelection } from '../src/promptReferenceDom'

/** Simulate a native contenteditable edit; setValue only supports form controls. */
export async function setPromptText(editor: DOMWrapper<Element>, value: string) {
  const element = editor.element as HTMLElement
  element.focus()
  element.textContent = value
  setPromptSelection(element, value.length)
  await editor.trigger('input')
}
