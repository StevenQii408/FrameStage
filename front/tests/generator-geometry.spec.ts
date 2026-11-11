import { describe, expect, it } from 'vitest'
import { generatorFrameSize } from '../src/generatorGeometry'
import { defaultGenerationSettings } from '../src/api/generations'
import type { GeneratorDraft } from '../src/generatorGraph'

const draft: GeneratorDraft = { id: 'gen-a', kind: 'image', position: { x: 0, y: 0 }, manualPrompt: '', imageAssetId: null, imageWidth: 0, imageHeight: 0 }
describe('generator frame geometry', () => {
  it.each(['16:9', '9:16', '1:1', '4:3', '3:4'] as const)('preserves %s exactly within a shared 268px long edge', ratio => {
    const size = generatorFrameSize({ ...draft, generationSettings: { ...defaultGenerationSettings('image'), aspect_ratio: ratio } }, { width: 17, height: 900 })
    const [width, height] = ratio.split(':').map(Number)
    expect(size.width / size.height).toBeCloseTo(width / height, 12)
    expect(Math.max(size.width, size.height)).toBe(268)
  })
  it('defaults empty legacy nodes to 16:9, retains historical local media, and ignores invalid dimensions', () => {
    expect(generatorFrameSize(draft)).toEqual({ width: 268, height: 150.75 })
    const legacy = { ...draft, imageAssetId: 'local-a', imageWidth: 100, imageHeight: 200 }
    expect(generatorFrameSize(legacy)).toEqual({ width: 134, height: 268 })
    expect(generatorFrameSize(legacy, { width: 1920, height: 1080 })).toEqual({ width: 134, height: 268 })
    expect(generatorFrameSize({ ...draft, imageWidth: 100, imageHeight: 200 })).toEqual({ width: 268, height: 150.75 })
    expect(generatorFrameSize(draft, { width: NaN, height: 20 }, NaN)).toEqual({ width: 268, height: 150.75 })
  })
})
