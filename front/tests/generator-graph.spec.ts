import { describe, expect, it } from 'vitest'
import { connectionSources, detachConnectionsFromSources, generatorReferenceId, missingReferenceMentions, physicalGeneratorHandles, rememberGeneratorReferences, removeGeneratorConnection, resolveGenerator, validateGeneratorConnection, type GeneratorDraft, type GeneratorConnection } from '../src/generatorGraph'

const image: GeneratorDraft = { id: 'gen-image', kind: 'image', nodeMode: 'generator', position: { x: 0, y: 0 }, manualPrompt: '手填图片', imageAssetId: 'asset-a', imageWidth: 768, imageHeight: 1366 }
const video: GeneratorDraft = { id: 'gen-video', kind: 'video', position: { x: 300, y: 0 }, manualPrompt: '手填视频', imageAssetId: 'asset-b', imageWidth: 1920, imageHeight: 1080 }
const sources = connectionSources([{ id: 'prompt-image', type: 'first_frame' }, { id: 'prompt-video', type: 'video' }], [{ id: 'shot-a', frames: [{}] }, { id: 'shot-empty', frames: [] }], [image, video])
const check = (source: string, sourceHandle: string, target: string, targetHandle: string, edges: GeneratorConnection[] = []) =>
  validateGeneratorConnection({ source, sourceHandle, target, targetHandle }, edges, [image, video], sources)

describe('typed generator connections', () => {
  it('routes a single physical input to typed slots and permits distinct image references', () => {
    const candidate = (source: string, sourceHandle: string) => ({ source, sourceHandle, target: video.id, targetHandle: 'input' })
    const kinds = connectionSources([{ id: 'prompt-video', type: 'video' }], [{ id: 'frame-a', frames: [{}] }, { id: 'frame-b', frames: [{}] }], [image, video])
    const prompt = validateGeneratorConnection(candidate('prompt-video', 'prompt-out'), [], [image, video], kinds).connection!
    const first = validateGeneratorConnection(candidate('frame-a', 'frame-out'), [prompt], [image, video], kinds).connection!
    const second = validateGeneratorConnection(candidate('frame-b', 'frame-out'), [prompt, first], [image, video], kinds).connection!
    expect(prompt.targetHandle).toBe('prompt-in')
    expect([first.targetHandle, second.targetHandle]).toEqual(['image-in', 'image-in'])
    expect(first.id).not.toBe(second.id)
    expect(physicalGeneratorHandles(first).targetHandle).toBe('input')
    expect(validateGeneratorConnection(candidate(image.id, 'output'), [], [image, video], kinds).connection?.sourceHandle).toBe('image-out')
    expect(validateGeneratorConnection(candidate('frame-a', 'frame-out'), [first], [image, video], kinds).ok).toBe(false)
  })
  it('keeps mention identities stable when a preceding reference is removed', () => {
    const item = { ...video, imageAssetId: null }
    const frames = { a: { url: '/a.jpg', width: 100, height: 100 }, b: { url: '/b.jpg', width: 100, height: 100 } }
    const wires: GeneratorConnection[] = ['a', 'b'].map(source => ({ id: 'wire-' + source, source, sourceHandle: 'frame-out', target: item.id, targetHandle: 'image-in' }))
    const initial = resolveGenerator(item.id, [item], wires, {}, frames, {})
    rememberGeneratorReferences(item, initial.references!)
    const after = resolveGenerator(item.id, [item], wires.slice(1), {}, frames, {})
    expect(after.references?.[0].mention).toBe('图片2')
    expect(after.references?.[0].id).toBe(generatorReferenceId('wire:b:frame-out'))
    expect(missingReferenceMentions('@图片1 @图片2', after.references!)).toEqual(['@图片1'])
    expect(item.referenceAliases).toEqual(expect.objectContaining({ [initial.references![0].id]: '图片1' }))
  })
  it('edits a connected Prompt locally without rewriting upstream or hiding source revisions', () => {
    const item: GeneratorDraft = { ...image, connectedPromptOverride: { sourceId: 'p', sourceText: '上游原文', text: '本节点改写' } }
    const wire: GeneratorConnection = { id: 'p', source: 'p', sourceHandle: 'prompt-out', target: item.id, targetHandle: 'prompt-in' }
    const prompts = { p: '上游原文' }
    expect(resolveGenerator(item.id, [item], [wire], prompts, {}, {}).prompt).toBe('本节点改写')
    expect(prompts.p).toBe('上游原文')
    expect(resolveGenerator(item.id, [item], [wire], { p: '上游新版本' }, {}, {}).prompt).toBe('上游新版本')
    expect(resolveGenerator(item.id, [item], [], prompts, {}, {}).prompt).toBe(item.manualPrompt)
  })
  it('accepts image/video Prompt and cross-task image references on their own ports', () => {
    expect(check('prompt-image', 'prompt-out', image.id, 'prompt-in').ok).toBe(true)
    expect(check('prompt-video', 'prompt-out', video.id, 'prompt-in').ok).toBe(true)
    expect(check('shot-a', 'frame-out', video.id, 'image-in').ok).toBe(true)
    expect(check(image.id, 'image-out', video.id, 'image-in').ok).toBe(true)
  })

  it('rejects mismatches, empty frames, duplicate ports and self connections', () => {
    expect(check('prompt-video', 'prompt-out', image.id, 'prompt-in').ok).toBe(false)
    expect(check('shot-empty', 'frame-out', image.id, 'image-in').ok).toBe(false)
    expect(check(image.id, 'image-out', image.id, 'image-in').ok).toBe(false)
    const existing = check('prompt-image', 'prompt-out', image.id, 'prompt-in').connection!
    expect(check('prompt-image', 'prompt-out', image.id, 'prompt-in', [existing]).ok).toBe(false)
    expect(check(video.id, 'video-out', image.id, 'image-in').ok).toBe(false)
  })

  it('rejects image generator cycles', () => {
    const second: GeneratorDraft = { ...image, id: 'gen-second' }
    const kinds = connectionSources([], [], [image, second])
    const edge = validateGeneratorConnection({ source: image.id, sourceHandle: 'image-out', target: second.id, targetHandle: 'image-in' }, [], [image, second], kinds).connection!
    const reverse = validateGeneratorConnection({ source: second.id, sourceHandle: 'image-out', target: image.id, targetHandle: 'image-in' }, [edge], [image, second], kinds)
    expect(reverse.reason).toContain('循环')
  })

  it('deletes only the selected manual wire and keeps the other input connected', () => {
    const promptWire = check('prompt-image', 'prompt-out', image.id, 'prompt-in').connection!
    const imageWire = check('shot-a', 'frame-out', image.id, 'image-in').connection!
    expect(removeGeneratorConnection([promptWire, imageWire], imageWire.id)).toEqual([promptWire])
  })

  it('preserves local nodes while detaching deleted task sources for undo', () => {
    const taskWire = check('prompt-image', 'prompt-out', image.id, 'prompt-in').connection!
    const localWire = check(image.id, 'image-out', video.id, 'image-in').connection!
    const result = detachConnectionsFromSources([taskWire, localWire], new Set(['prompt-image']))
    expect(result.detached).toEqual([taskWire])
    expect(result.kept).toEqual([localWire])
  })

  it('passes an imported image through an image node into a video node', () => {
    const wire = check(image.id, 'image-out', video.id, 'image-in').connection!
    const linked = resolveGenerator(video.id, [image, video], [wire], {}, {}, { 'asset-a': 'blob:portrait', 'asset-b': 'blob:backup' })
    expect(linked).toMatchObject({ imageUrl: 'blob:portrait', imageSource: '已连接图片节点', imageWidth: 768, imageHeight: 1366, ownImageUrl: 'blob:backup', ownImageWidth: 1920 })
    const fallback = resolveGenerator(video.id, [image, video], [], {}, {}, { 'asset-a': 'blob:portrait', 'asset-b': 'blob:backup' })
    expect(fallback.imageUrl).toBe('blob:backup')
  })

  it('does not forward a reference image as an image-node output', () => {
    const source = { ...image, imageAssetId: null }
    const target = { ...video, imageAssetId: null }
    const first = { id: 'first', source: 'shot-a', sourceHandle: 'frame-out' as const, target: source.id, targetHandle: 'image-in' as const }
    const second = { id: 'second', source: source.id, sourceHandle: 'image-out' as const, target: target.id, targetHandle: 'image-in' as const }
    const result = resolveGenerator(target.id, [source, target], [first, second], {}, { 'shot-a': { url: '/frame.jpg', width: 1080, height: 1920 } }, {})
    expect(result.imageUrl).toBe('')
    expect(result.ownImageUrl).toBe('')
  })

  it('follows connected values and falls back to manual Prompt/local image when disconnected', () => {
    const promptWire = check('prompt-image', 'prompt-out', image.id, 'prompt-in').connection!
    const frameWire = check('shot-a', 'frame-out', image.id, 'image-in').connection!
    const props = { prompts: { 'prompt-image': '已连接首帧' }, frames: { 'shot-a': { url: '/frame.jpg', width: 1080, height: 1920 } }, assets: { 'asset-a': 'blob:local' } }
    expect(resolveGenerator(image.id, [image], [promptWire, frameWire], props.prompts, props.frames, props.assets)).toMatchObject({ prompt: '已连接首帧', imageUrl: '/frame.jpg', imageWidth: 1080, ownImageUrl: 'blob:local', ownImageWidth: 768 })
    expect(resolveGenerator(image.id, [image], [], props.prompts, props.frames, props.assets)).toMatchObject({ prompt: '手填图片', imageUrl: 'blob:local', imageHeight: 1366 })
  })
})
