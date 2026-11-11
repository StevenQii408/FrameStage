import { describe, expect, it } from 'vitest'
import { visibleCanvasPrompts } from '../src/promptVisibility'
import { applyDarkTheme, DARK_THEME_COLOR } from '../src/theme'
import type { Prompt } from '../src/types'

function prompt(id: string, type: Prompt['type'], platform: string, templateId: string): Prompt {
  return { id, shotId: 'shot-1', type, platform, templateId, templateVersion: 1, version: 1, text: id, status: 'ready', warnings: [] }
}

describe('production prompt pair and default theme', () => {
  it('shows only first-frame and Seedance for legacy four-profile jobs', () => {
    const prompts = [
      prompt('first', 'first_frame', 'generic-image', 'first-frame-v1'),
      prompt('image', 'image', 'generic-image', 'generic-image-v1'),
      prompt('seedance', 'video', 'seedance', 'seedance-video-v1'),
      prompt('kling', 'video', 'kling', 'kling-video-v1'),
    ]
    expect(visibleCanvasPrompts(prompts).map((item) => item.id)).toEqual(['first', 'seedance'])
  })

  it('always initializes a reload in dark mode', () => {
    document.documentElement.style.colorScheme = 'light'
    const meta = document.createElement('meta')
    meta.name = 'theme-color'
    meta.content = '#ffffff'
    document.head.append(meta)
    applyDarkTheme()
    expect(document.documentElement.style.colorScheme).toBe('dark')
    expect(meta.content).toBe(DARK_THEME_COLOR)
    meta.remove()
  })
})
