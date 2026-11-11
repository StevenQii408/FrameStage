export interface PreviewSize { width: number; height: number }

function positive(value: number, fallback: number): number {
  return Number.isFinite(value) && value > 0 ? value : fallback
}

/** A transient preview uses real media proportions and visible screen space.
 * Returned dimensions are Vue Flow units; generation settings remain unchanged.
 */
export function generatorPreviewSize(media: PreviewSize, viewport: PreviewSize, zoom = 1): PreviewSize {
  const complete = Number.isFinite(media.width) && media.width > 0 && Number.isFinite(media.height) && media.height > 0
  const width = complete ? media.width : 16
  const height = complete ? media.height : 9
  const scale = Math.min(
    Math.max(160, Math.min(1280, positive(viewport.width, 1280) - 96)) / width,
    Math.max(120, Math.min(900, positive(viewport.height, 800) - 140)) / height,
  ) / positive(zoom, 1)
  return { width: width * scale, height: height * scale }
}
