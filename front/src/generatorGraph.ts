import type { GenerationSettings } from './api/generations'
import { activeGeneratorAsset, activeGeneratorVersion, isReferenceAsset, type GeneratorVersion } from './generatorVersions'
export type { GeneratorVersion } from './generatorVersions'

export type GeneratorKind = 'image' | 'video'
export type GeneratorPort = 'input' | 'output' | 'prompt-in' | 'image-in' | 'prompt-out' | 'frame-out' | 'image-out' | 'video-out'
export type SourceKind = 'prompt-image' | 'prompt-video' | 'frame' | 'image' | 'video'

export interface GeneratorDraft {
  id: string
  kind: GeneratorKind
  position: { x: number; y: number }
  manualPrompt: string
  imageAssetId: string | null
  imageWidth: number
  imageHeight: number
  nodeMode?: 'generator' | 'reference'
  versions?: GeneratorVersion[]
  activeVersionId?: string
  generationSettings?: GenerationSettings
  generationTask?: { jobId: string; inputSignature: string; eventCursor?: number }
  pendingSubmission?: { idempotencyKey: string; inputSignature: string; retryOf?: string }
  referenceImages?: Array<{ assetId: string; width: number; height: number }>
  referenceSelections?: Record<string, boolean>
  referenceAliases?: Record<string, string>
  firstFrameReferenceId?: string
  connectedPromptOverride?: { sourceId: string; sourceText: string; text: string }
}

export interface GeneratorConnection {
  id: string
  source: string
  sourceHandle: GeneratorPort
  target: string
  targetHandle: 'prompt-in' | 'image-in'
}

export interface ConnectionCandidate {
  source: string
  sourceHandle: string | null
  target: string
  targetHandle: string | null
}

export interface ConnectionSource {
  kind: SourceKind
  hasImage?: boolean
}

export function detachConnectionsFromSources(connections: GeneratorConnection[], removedSourceIds: Set<string>): { kept: GeneratorConnection[]; detached: GeneratorConnection[] } {
  return { kept: connections.filter((edge) => !removedSourceIds.has(edge.source)), detached: connections.filter((edge) => removedSourceIds.has(edge.source)) }
}

export function removeGeneratorConnection(connections: GeneratorConnection[], id: string): GeneratorConnection[] {
  return connections.filter((wire) => wire.id !== id)
}

export function connectionId(target: string, targetHandle: string, source?: string): string {
  return 'wire-' + target + '-' + targetHandle + (source ? '-' + source : '')
}

export const physicalGeneratorHandles = (wire: GeneratorConnection) => ({
  sourceHandle: wire.source.startsWith('gen-') ? 'output' : wire.sourceHandle, targetHandle: 'input',
})

export function generatorAssetIds(item: GeneratorDraft): string[] {
  return [...new Set([item.imageAssetId, ...(item.referenceImages || []).map(image => image.assetId)].filter((id): id is string => Boolean(id)))]
}

export function generatorReferenceId(identity: string): string {
  let a = 2166136261, b = 5381
  for (let n = 0; n < identity.length; n++) { a = Math.imul(a ^ identity.charCodeAt(n), 16777619); b = Math.imul(b, 33) ^ identity.charCodeAt(n) }
  return 'ref-' + (a >>> 0).toString(16).padStart(8, '0') + (b >>> 0).toString(16).padStart(8, '0').slice(0, 4)
}

export function validateGeneratorConnection(
  candidate: ConnectionCandidate,
  connections: GeneratorConnection[],
  generators: GeneratorDraft[],
  sources: Record<string, ConnectionSource>,
): { ok: boolean; reason: string; connection?: GeneratorConnection } {
  const target = generators.find((item) => item.id === candidate.target)
  if (!target) return { ok: false, reason: '只能连接到图片或视频生成节点的输入端口' }
  if (isReferenceAsset(target)) return { ok: false, reason: '上传图片是只读参考资产，请连接到其他生成节点的输入端口' }
  if (candidate.source === candidate.target) return { ok: false, reason: '节点不能连接自身' }
  const source = sources[candidate.source]
  if (!source) return { ok: false, reason: '来源节点没有可用的输出' }
  const output = candidate.sourceHandle === 'output' ? (source.kind === 'image' ? 'image-out' : source.kind === 'video' ? 'video-out' : candidate.sourceHandle) : candidate.sourceHandle
  const input = candidate.targetHandle === 'input' ? (source.kind.startsWith('prompt-') ? 'prompt-in' : 'image-in') : candidate.targetHandle
  if (input !== 'prompt-in' && input !== 'image-in') return { ok: false, reason: '请选择生成节点左侧的输入端口' }
  if (connections.some((edge) => edge.target === target.id && edge.targetHandle === input && (input === 'prompt-in' || edge.source === candidate.source))) return { ok: false, reason: '此输入端口已有连线，请先断开' }
  if (input === 'image-in' && connections.filter(edge => edge.target === target.id && edge.targetHandle === input).length >= 8) return { ok: false, reason: '每个生成节点最多连接八张参考图' }
  const validPrompt = input === 'prompt-in' && output === 'prompt-out' && source.kind === (target.kind === 'image' ? 'prompt-image' : 'prompt-video')
  const validImage = input === 'image-in' && (
    (output === 'frame-out' && source.kind === 'frame' && source.hasImage !== false) ||
    (output === 'image-out' && source.kind === 'image')
  )
  if (!validPrompt && !validImage) return { ok: false, reason: '端口类型不匹配：图片接图片端口，所属类型的 Prompt 接文字端口' }
  if (input === 'image-in' && source.kind === 'image') {
    const stack = [target.id]
    const seen = new Set<string>()
    while (stack.length) {
      const id = stack.pop()!
      if (id === candidate.source) return { ok: false, reason: '连线会形成循环' }
      if (seen.has(id)) continue
      seen.add(id)
      for (const edge of connections) if (edge.source === id && edge.sourceHandle === 'image-out') stack.push(edge.target)
    }
  }
  return { ok: true, reason: '', connection: { id: connectionId(target.id, input, input === 'image-in' ? candidate.source : undefined), source: candidate.source, sourceHandle: output as GeneratorPort, target: target.id, targetHandle: input } }
}

export interface GeneratorReference {
  id: string
  mention: string
  url: string
  width: number
  height: number
  identity: string
  source: string
  sourceNodeId?: string
  localAssetId?: string
  selected: boolean
  firstFrame?: boolean
}

export function rememberGeneratorReferences(item: GeneratorDraft, references: GeneratorReference[]): void {
  if (!references.length) return
  item.referenceAliases = { ...item.referenceAliases, ...Object.fromEntries(references.map(reference => [reference.id, reference.mention])) }
}

export function missingReferenceMentions(prompt: string, references: GeneratorReference[]): string[] {
  const available = new Set(references.filter(reference => reference.selected && reference.url).map(reference => reference.mention))
  return [...new Set((prompt.match(/@图片\d+/g) || []).filter(mention => !available.has(mention.slice(1))))]
}

export interface ResolvedGenerator {
  prompt: string
  promptSource: string
  imageUrl: string
  imageSource: string
  imageWidth: number
  imageHeight: number
  ownImageUrl: string
  ownImageWidth: number
  ownImageHeight: number
  imageIdentity?: string
  references?: GeneratorReference[]
  promptSourceId?: string
  sourcePrompt?: string
}

/** Protect enrichment callbacks too: persisted/corrupt wires may bypass UI validation. */
export function createGeneratorResolutionGuard() {
  const resolving = new Set<string>()
  return <T>(id: string, fallback: T, resolve: () => T): T => {
    if (resolving.has(id)) return fallback
    resolving.add(id)
    try { return resolve() } finally { resolving.delete(id) }
  }
}

/** A connected input wins; a missing upstream asset falls back to the local import. */
export function resolveGenerator(
  id: string,
  generators: GeneratorDraft[],
  connections: GeneratorConnection[],
  prompts: Record<string, string>,
  frames: Record<string, { url: string; width: number; height: number }>,
  assets: Record<string, string>,
  visited = new Set<string>(),
): ResolvedGenerator {
  const item = generators.find((node) => node.id === id)
  if (!item || visited.has(id)) return { prompt: '', promptSource: '', imageUrl: '', imageSource: '', imageWidth: 0, imageHeight: 0, ownImageUrl: '', ownImageWidth: 0, ownImageHeight: 0 }
  visited.add(id)
  const inputConnections = isReferenceAsset(item) ? [] : connections
  const promptEdge = inputConnections.find((edge) => edge.target === id && edge.targetHandle === 'prompt-in')
  let prompt = item.manualPrompt
  let promptSource = '手动输入'
  if (promptEdge && prompts[promptEdge.source] !== undefined) {
    prompt = prompts[promptEdge.source]
    promptSource = '已连接 Prompt'
    const edited = item.connectedPromptOverride
    if (edited?.sourceId === promptEdge.source && edited.sourceText === prompt) {
      prompt = edited.text
      promptSource = '已连接 Prompt · 本地编辑'
    }
  }
  const output = activeGeneratorAsset(item)
  const ownOutput = output?.mime_type.startsWith('image/') ? output : null
  const ownImageUrl = ownOutput?.url || (item.imageAssetId ? assets[item.imageAssetId] || '' : '')
  const ownImageWidth = ownImageUrl ? ownOutput?.width || item.imageWidth : 0
  const ownImageHeight = ownImageUrl ? ownOutput?.height || item.imageHeight : 0
  const references: GeneratorReference[] = []
  for (const edge of inputConnections.filter(wire => wire.target === id && wire.targetHandle === 'image-in')) {
    const frame = frames[edge.source]
    const upstream = generators.find(node => node.id === edge.source)
    const active = upstream ? activeGeneratorAsset(upstream) : null
    const version = upstream ? activeGeneratorVersion(upstream) : null
    const generated = active?.mime_type.startsWith('image/') ? active : null
    const local = upstream?.imageAssetId ? assets[upstream.imageAssetId] : ''
    const source = frame || generated || (local && upstream ? { url: local, width: upstream.imageWidth, height: upstream.imageHeight } : null)
    references.push({ id: generatorReferenceId('wire:' + edge.source + ':' + edge.sourceHandle), mention: '',
      url: source?.url || '', width: source?.width || 0, height: source?.height || 0, identity: frame ? frame.url : generated ? 'version:' + version!.id + ':' + generated.id : local ? 'local:' + upstream!.imageAssetId : 'wire:' + edge.source,
      source: frame ? '已连接分镜首帧' : '已连接图片节点', sourceNodeId: edge.source, selected: true })
  }
  const localImages = [...(item.imageAssetId ? [{ assetId: item.imageAssetId, width: item.imageWidth, height: item.imageHeight }] : []), ...(item.referenceImages || [])]
  const seenAssets = new Set<string>()
  for (const image of localImages) {
    if (seenAssets.has(image.assetId)) continue
    seenAssets.add(image.assetId)
    if (assets[image.assetId]) references.push({ id: generatorReferenceId('local:' + image.assetId), mention: '', url: assets[image.assetId],
      width: image.width, height: image.height, identity: 'local:' + image.assetId, source: '本地导入图片', localAssetId: image.assetId, selected: true })
  }
  const taken = new Set(Object.values(item.referenceAliases || {}))
  let next = 1
  for (const reference of references) {
    reference.mention = item.referenceAliases?.[reference.id] || ''
    if (!reference.mention) {
      while (taken.has('图片' + next)) next++
      reference.mention = '图片' + next++
      taken.add(reference.mention)
    }
    reference.selected = item.referenceSelections?.[reference.id] !== false
  }
  const selected = references.filter(reference => reference.selected)
  const first = selected.find(reference => reference.id === item.firstFrameReferenceId) || selected.find(reference => reference.url) || selected[0]
  if (first) first.firstFrame = item.kind === 'video'
  return { prompt, promptSource, imageUrl: first?.url || '', imageSource: first?.source || '', imageWidth: first?.width || 0,
    imageHeight: first?.height || 0, ownImageUrl, ownImageWidth, ownImageHeight, imageIdentity: first?.identity || '', references,
    ...(promptEdge ? { promptSourceId: promptEdge.source, sourcePrompt: prompts[promptEdge.source] } : {}) }
}

export function connectionSources(
  prompts: Array<{ id: string; type: string }>,
  shots: Array<{ id: string; frames: unknown[] }>,
  generators: GeneratorDraft[],
): Record<string, ConnectionSource> {
  const result: Record<string, ConnectionSource> = {}
  prompts.forEach((item) => { result[item.id] = { kind: item.type === 'video' ? 'prompt-video' : 'prompt-image' } })
  shots.forEach((item) => { result[item.id] = { kind: 'frame', hasImage: item.frames.length > 0 } })
  generators.forEach((item) => { result[item.id] = { kind: item.kind } })
  return result
}

