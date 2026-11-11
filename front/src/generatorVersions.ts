import { generationAssetUrl, type GenerationAsset, type GenerationJob, type GenerationSettings } from './api/generations'
import type { GeneratorDraft, GeneratorKind } from './generatorGraph'

export interface GeneratorVersion {
  readonly id: string
  readonly jobId: string
  readonly createdAt: string
  readonly asset: Readonly<GenerationAsset>
  readonly prompt: string
  readonly settings: Readonly<GenerationSettings>
  readonly inputSignature?: string
}

type VersionedNode = Pick<GeneratorDraft, 'kind' | 'imageAssetId'> & Partial<Pick<GeneratorDraft,
  'nodeMode' | 'generationTask' | 'pendingSubmission' | 'versions' | 'activeVersionId'>>

/** Legacy imports without an actual generation attempt are reference assets. */
export function isReferenceAsset(item: VersionedNode): boolean {
  if (item.kind !== 'image') return false
  if (item.nodeMode) return item.nodeMode === 'reference'
  return item.kind === 'image' && Boolean(item.imageAssetId) && !item.generationTask && !item.pendingSubmission && !item.versions?.length
}

export function activeGeneratorVersion(item: Pick<GeneratorDraft, 'versions' | 'activeVersionId'>): GeneratorVersion | null {
  return item.versions?.find(version => version.id === item.activeVersionId) || item.versions?.[0] || null
}

function localAssetUrl(raw: string): string {
  try { return generationAssetUrl(raw) } catch { return '' }
}

function validAsset(value: unknown, kind: GeneratorKind): value is GenerationAsset {
  if (!value || typeof value !== 'object') return false
  const asset = value as GenerationAsset
  const url = typeof asset.url === 'string' ? localAssetUrl(asset.url) : ''
  return typeof asset.id === 'string' && /^genasset-[a-f0-9]{64}$/.test(asset.id)
    && typeof asset.mime_type === 'string' && asset.mime_type.startsWith(kind + '/')
    && Number.isFinite(asset.width) && asset.width > 0 && Number.isFinite(asset.height) && asset.height > 0
    && Boolean(url) && new URL(url).pathname === '/api/generation-assets/' + asset.id
}

function newestFirst(a: GeneratorVersion, b: GeneratorVersion): number {
  const milliseconds = Date.parse(b.createdAt) - Date.parse(a.createdAt)
  if (milliseconds) return milliseconds
  // SQLite/Python timestamps carry microseconds; Date.parse truncates at ms.
  // Compare the remaining decimal fraction without float rounding or ID order.
  const remainder = (value: string) => (value.match(/\.(\d+)(?:Z|[+-]\d{2}:?\d{2})$/i)?.[1] || '').slice(3)
  const left = remainder(a.createdAt), right = remainder(b.createdAt)
  const precision = Math.max(left.length, right.length)
  const aFraction = left.padEnd(precision, '0'), bFraction = right.padEnd(precision, '0')
  return aFraction < bFraction ? 1 : aFraction > bFraction ? -1 : 0
}

/** Validate persisted history without rewriting a version's prompt or settings. */
export function readGeneratorVersions(raw: unknown, kind: GeneratorKind): GeneratorVersion[] {
  if (!Array.isArray(raw)) return []
  const seen = new Set<string>()
  const result: GeneratorVersion[] = []
  for (const value of raw) {
    if (!value || typeof value !== 'object') continue
    const item = value as GeneratorVersion
    if (typeof item.id !== 'string' || !item.id || item.id.length > 240 || seen.has(item.id)
      || typeof item.jobId !== 'string' || !item.jobId.startsWith('generation-')
      || typeof item.createdAt !== 'string' || !Number.isFinite(Date.parse(item.createdAt))
      || typeof item.prompt !== 'string' || !validAsset(item.asset, kind)
      || !item.settings || typeof item.settings !== 'object') continue
    const settings = item.settings
    if (!['21:9', '16:9', '9:16', '1:1', '4:3', '3:4'].includes(settings.aspect_ratio)
      || !['480p', '512p', '720p', '1080p', '1536p', '2048p', '2160p'].includes(settings.resolution)
      || (settings.duration_seconds !== null && (typeof settings.duration_seconds !== 'number' || !Number.isFinite(settings.duration_seconds)))) continue
    seen.add(item.id)
    const saved = JSON.parse(JSON.stringify(item)) as GeneratorVersion
    result.push({ ...saved, asset: { ...saved.asset, url: localAssetUrl(saved.asset.url) } })
  }
  return result.sort(newestFirst)
}

/** Only a verified, successful backend job adds history; failures keep old media. */
export function recordGeneratorSuccess(item: GeneratorDraft, job: GenerationJob, inputSignature?: string): boolean {
  if (isReferenceAsset(item) || job.status !== 'succeeded' || job.node_id !== item.id || job.kind !== item.kind
    || !Array.isArray(job.assets) || typeof job.request?.prompt !== 'string' || !job.request?.settings) return false
  const versions = job.assets.filter(asset => validAsset(asset, item.kind)).map(asset => ({
    id: job.id + ':' + asset.id, jobId: job.id, createdAt: job.updated_at || job.created_at,
    asset: { ...asset }, prompt: job.request.prompt, settings: { ...job.request.settings },
    ...(inputSignature ? { inputSignature } : {}),
  } satisfies GeneratorVersion)).filter(version => !item.versions?.some(previous => previous.id === version.id))
  if (!versions.length) return false
  // Equal delivery times retain this explicit newest-observed-first order.
  // Stable sorting then preserves it across saves/reloads, independent of IDs.
  const next = readGeneratorVersions([...versions, ...(item.versions || [])], item.kind)
  const added = new Set(versions.map(version => version.id))
  if (!next.some(value => added.has(value.id))) return false
  item.nodeMode = 'generator'
  item.versions = next
  if (added.has(next[0].id) || !item.activeVersionId) item.activeVersionId = next[0].id
  return true
}

/** Selection changes the output asset only; editable drafts remain untouched. */
export function selectGeneratorVersion(item: GeneratorDraft, id: string): boolean {
  if (!item.versions?.some(version => version.id === id) || item.activeVersionId === id) return false
  item.activeVersionId = id
  return true
}

export function activeGeneratorAsset(item: Pick<GeneratorDraft, 'versions' | 'activeVersionId'>): GenerationAsset | null {
  const version = activeGeneratorVersion(item)
  if (!version) return null
  const url = localAssetUrl(version.asset.url)
  return url ? { ...version.asset, url } : null
}
