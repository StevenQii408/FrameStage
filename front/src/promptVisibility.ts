import type { Prompt } from './types'

/** New jobs only contain these two profiles; old four-profile jobs stay readable
 * and exportable, while the canvas deliberately presents the production pair. */
export function visibleCanvasPrompts(prompts: Prompt[]): Prompt[] {
  return prompts.filter((prompt) => (
    prompt.templateId === 'first-frame-v1'
    || prompt.templateId === 'seedance-video-v1'
    || prompt.type === 'first_frame'
    || (prompt.type === 'video' && prompt.platform.toLowerCase() === 'seedance')
  ))
}
