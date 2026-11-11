import { defineComponent, h, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { imageDimensions, loadLocalImage, saveLocalImage } from '../src/generatorStorage'
import { useGeneratorAssets } from '../src/composables/useGeneratorAssets'
import type { GeneratorDraft } from '../src/generatorGraph'
import { isReferenceAsset } from '../src/generatorVersions'
import { generatorFrameSize } from '../src/generatorGeometry'
import { defaultGenerationSettings } from '../src/api/generations'

vi.mock('../src/generatorStorage', () => ({ loadLocalImage: vi.fn(), saveLocalImage: vi.fn(), validateLocalImage: vi.fn(), imageDimensions: vi.fn() }))
describe('reference asset lifecycle', () => {
  it('keeps the newest multi-image hydration and releases replaced URLs', async () => {
    const generators = ref<GeneratorDraft[]>([{ id: 'gen-a', kind: 'image', position: { x: 0, y: 0 }, manualPrompt: '', imageAssetId: 'a', imageWidth: 100, imageHeight: 100,
      referenceImages: [{ assetId: 'b', width: 100, height: 100 }] }])
    let first!: (blob: Blob) => void
    vi.mocked(loadLocalImage).mockImplementationOnce(() => new Promise(resolve => { first = resolve }))
      .mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
    const revoke = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn().mockReturnValueOnce('blob:a').mockReturnValueOnce('blob:b') })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revoke })
    let assets!: ReturnType<typeof useGeneratorAssets>
    const wrapper = mount(defineComponent({ setup() { assets = useGeneratorAssets(generators, vi.fn(), vi.fn(), vi.fn()); return () => h('div') } }))
    const obsolete = assets.hydrateAssets()
    await assets.hydrateAssets()
    first(new Blob(['late'], { type: 'image/png' }))
    await obsolete
    expect(assets.assetUrls.value).toEqual({ a: 'blob:a', b: 'blob:b' })
    generators.value[0].imageAssetId = null
    await assets.hydrateAssets()
    expect(assets.assetUrls.value).toEqual({ b: 'blob:b' })
    expect(revoke).toHaveBeenCalledWith('blob:a')
    wrapper.unmount()
    expect(revoke).toHaveBeenCalledWith('blob:b')
  })

  function importer(kind: 'image' | 'video' = 'image', extra: Partial<GeneratorDraft> = {}) {
    vi.mocked(loadLocalImage).mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
    vi.mocked(saveLocalImage).mockResolvedValue()
    vi.mocked(imageDimensions).mockResolvedValue({ width: 1200, height: 800 })
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:' + crypto.randomUUID()) })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    const generators = ref<GeneratorDraft[]>([{ id: 'gen-import', kind, position: { x: 0, y: 0 }, manualPrompt: '', imageAssetId: null,
      imageWidth: 0, imageHeight: 0, generationSettings: { ...defaultGenerationSettings(kind), aspect_ratio: '9:16' }, ...extra }])
    let assets!: ReturnType<typeof useGeneratorAssets>
    const snapshots: GeneratorDraft[][] = []
    const snapshot = vi.fn(() => snapshots.push(JSON.parse(JSON.stringify(generators.value))))
    const wrapper = mount(defineComponent({ setup() { assets = useGeneratorAssets(generators, vi.fn(), snapshot, vi.fn()); return () => h('div') } }))
    return { generators, assets, wrapper, snapshots }
  }

  it('saves a captured frame atomically as a separate fixed-ratio reference with one undo snapshot', async () => {
    const { generators, assets, wrapper, snapshots } = importer('video')
    const original = JSON.stringify(generators.value[0])
    const image = new File(['actual decoded pixels'], 'current-frame.png', { type: 'image/png' })
    const result = await assets.addReferenceImage(image, { x: 400, y: 50 })
    expect(result?.nodeMode).toBe('reference')
    expect(result?.position).toEqual({ x: 400, y: 50 })
    expect(result?.generationTask).toBeUndefined()
    expect(result?.versions).toBeUndefined()
    expect(generators.value).toHaveLength(2)
    expect(JSON.stringify(generators.value[0])).toBe(original)
    expect(snapshots).toHaveLength(1)
    expect(snapshots[0]).toHaveLength(1)
    expect(saveLocalImage).toHaveBeenCalledWith(result?.imageAssetId, image)
    expect(generatorFrameSize(result!).width / generatorFrameSize(result!).height).toBe(1.5)
    wrapper.unmount()
  })

  it('does not create an empty reference node when the captured PNG cannot be persisted', async () => {
    const { generators, assets, wrapper, snapshots } = importer('video')
    vi.mocked(saveLocalImage).mockRejectedValueOnce(new Error('quota exceeded'))
    expect(await assets.addReferenceImage(new File(['pixels'], 'frame.png', { type: 'image/png' }), { x: 0, y: 0 })).toBeNull()
    expect(generators.value).toHaveLength(1)
    expect(snapshots).toHaveLength(0)
    wrapper.unmount()
  })

  it('converts a manual image upload into a fixed-ratio reference asset', async () => {
    const { generators, assets, wrapper } = importer()
    await assets.importGeneratorImage('gen-import', new File(['png'], 'reference.png', { type: 'image/png' }))
    const item = generators.value[0]
    expect(isReferenceAsset(item)).toBe(true)
    expect(item.nodeMode).toBe('reference')
    expect(generatorFrameSize(item).width / generatorFrameSize(item).height).toBe(1.5)
    expect(item.versions).toBeUndefined()
    wrapper.unmount()
  })

  it('keeps an uploaded video-node image as a first-frame reference, never a video output', async () => {
    const { generators, assets, wrapper } = importer('video')
    await assets.importGeneratorImage('gen-import', new File(['png'], 'first-frame.png', { type: 'image/png' }))
    const item = generators.value[0]
    expect(item.nodeMode).toBe('generator')
    expect(isReferenceAsset(item)).toBe(false)
    expect(item.imageAssetId).toMatch(/^image-/)
    expect(item.versions).toBeUndefined()
    expect(generatorFrameSize(item).width / generatorFrameSize(item).height).toBeCloseTo(9 / 16)
    wrapper.unmount()
  })

  it('replaces an asset image reversibly without deleting the old IndexedDB Blob', async () => {
    const { generators, assets, wrapper, snapshots } = importer('image', { nodeMode: 'reference', imageAssetId: 'old-import', imageWidth: 400, imageHeight: 800 })
    await assets.hydrateAssets()
    await assets.importGeneratorReference('gen-import', new File(['new'], 'replacement.png', { type: 'image/png' }))
    expect(generators.value[0].nodeMode).toBe('reference')
    expect(generators.value[0].referenceImages).toHaveLength(1)
    expect(snapshots[0][0].imageAssetId).toBe('old-import')
    generators.value = snapshots[0]
    await assets.hydrateAssets()
    expect(generators.value[0].imageAssetId).toBe('old-import')
    expect(assets.assetUrls.value['old-import']).toMatch(/^blob:/)
    expect(loadLocalImage).toHaveBeenCalledWith('old-import')
    wrapper.unmount()
  })

  it('does not convert a node after a generation submission starts during image decoding', async () => {
    const { generators, assets, wrapper } = importer()
    let decoded!: (value: { width: number; height: number }) => void
    vi.mocked(imageDimensions).mockImplementationOnce(() => new Promise(resolve => { decoded = resolve }))
    const importing = assets.importGeneratorImage('gen-import', new File(['png'], 'reference.png', { type: 'image/png' }))
    generators.value[0].generationTask = { jobId: 'generation-inflight', inputSignature: 'paid-snapshot' }
    decoded({ width: 1200, height: 800 }); await importing
    expect(generators.value[0].nodeMode).toBe('generator')
    expect(generators.value[0].generationTask?.jobId).toBe('generation-inflight')
    wrapper.unmount()
  })
})
