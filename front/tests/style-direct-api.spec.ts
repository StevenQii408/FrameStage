import { beforeEach, describe, expect, it, vi } from 'vitest'
const apiFetch = vi.hoisted(() => vi.fn())
vi.mock('../src/api/client', () => ({ apiFetch, apiUrl: (url: string) => url }))
import { editDirectStyle } from '../src/api/styleDirect'

describe('trusted recipe save request', () => {
  beforeEach(() => vi.clearAllMocks())
  it('sends the exact draft, optimistic version and trusted recipe provenance', () => {
    editDirectStyle('analysis/1', '完整原文\n下一段', 7, { kind: 'library-recipe', recipe_id: 'recipe-a', source_hash: 'hash-a' })
    const [url, request] = apiFetch.mock.calls[0]
    expect(url).toBe('/style/analyses/analysis%2F1/profile'); expect(request.method).toBe('PUT')
    expect(JSON.parse(request.body)).toEqual({ rendered_prompt: '完整原文\n下一段', expected_version: 7, source: { kind: 'library-recipe', recipe_id: 'recipe-a', source_hash: 'hash-a' } })
  })
  it('keeps manual edits independent of recipe provenance', () => {
    editDirectStyle('analysis', '手工画风', 3)
    expect(JSON.parse(apiFetch.mock.calls[0][1].body)).toEqual({ rendered_prompt: '手工画风', expected_version: 3 })
  })
})
