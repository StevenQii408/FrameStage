import type { GeneratorDraft } from './generatorGraph'
import { activeGeneratorVersion, isReferenceAsset } from './generatorVersions'

type GeneratorGeometryInput = Pick<GeneratorDraft, 'generationSettings' | 'imageAssetId' | 'imageWidth' | 'imageHeight'>
  & Partial<Pick<GeneratorDraft, 'kind' | 'nodeMode' | 'generationTask' | 'pendingSubmission' | 'versions' | 'activeVersionId'>>
type ImageSize = { width: number; height: number }
const ratios: Record<string, ImageSize> = { '21:9': { width: 21, height: 9 }, '16:9': { width: 16, height: 9 }, '9:16': { width: 9, height: 16 }, '1:1': { width: 1, height: 1 }, '4:3': { width: 4, height: 3 }, '3:4': { width: 3, height: 4 } }
function validSize(value?: ImageSize | null): value is ImageSize {
  return Boolean(value && Number.isFinite(value.width) && value.width > 0 && Number.isFinite(value.height) && value.height > 0)
}

/** Frame only: the canvas and card share these dimensions; external labels are separate. */
export function generatorFrameSize(generator: GeneratorGeometryInput, image?: ImageSize | null, longEdge = 268): ImageSize {
  const explicit = generator.generationSettings?.aspect_ratio
  const legacy = validSize(image) ? image : generator.imageAssetId && validSize({ width: generator.imageWidth, height: generator.imageHeight })
    ? { width: generator.imageWidth, height: generator.imageHeight } : null
  const original = { width: generator.imageWidth, height: generator.imageHeight }
  const reference = isReferenceAsset({ ...generator, kind: generator.kind || 'image' })
  const active = activeGeneratorVersion(generator)?.asset
  const shape = reference ? validSize(original) ? original : legacy || ratios['16:9']
    : active && validSize(active) ? active : (explicit && ratios[explicit]) || legacy || ratios['16:9']
  const edge = Number.isFinite(longEdge) && longEdge > 0 ? longEdge : 268
  const scale = edge / Math.max(shape.width, shape.height)
  return { width: shape.width * scale, height: shape.height * scale }
}
