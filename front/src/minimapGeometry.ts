export type MiniPoint = { x: number; y: number }
export type MiniRect = MiniPoint & { width: number; height: number }
export type MiniNode = { position: MiniPoint; width: number; height: number }
export type MiniViewport = MiniPoint & { zoom: number }

export type MiniNodeKind = 'input' | 'shot' | 'image-generation' | 'video-generation' | 'scene' | 'prompt' | 'event-prompt' | 'other'

/** Only input tasks use a round marker; the other kinds share proportional rectangles. */
export function minimapNodeKind(node: { type?: string; data?: { generator?: { kind?: unknown } } }): MiniNodeKind {
  if (node.type === 'project') return 'input'
  if (node.type === 'shot' || node.type === 'sequence') return 'shot'
  if (node.type === 'generator' && node.data?.generator?.kind === 'image') return 'image-generation'
  if (node.type === 'generator' && node.data?.generator?.kind === 'video') return 'video-generation'
  if (node.type === 'scene') return 'scene'
  if (node.type === 'eventPrompt') return 'event-prompt'
  if (node.type === 'prompt') return 'prompt'
  return 'other'
}

export function minimapNodeSize(node: { dimensions?: { width?: unknown; height?: unknown }; width?: unknown; height?: unknown; data?: { layoutWidth?: unknown; layoutHeight?: unknown } }) {
  const positive = (values: unknown[], fallback: number) => values.map(value => typeof value === 'number' || typeof value === 'string' ? Number(value) : NaN).find(value => Number.isFinite(value) && value > 0) || fallback
  return {
    width: positive([node.dimensions?.width, node.data?.layoutWidth, node.width], 230),
    height: positive([node.dimensions?.height, node.data?.layoutHeight, node.height], 180),
  }
}

/** Apply one scale to both dimensions; hidden or narrow nodes keep their real proportions. */
export function minimapNodeRect(node: MiniNode, projection: { x: (value: number) => number; y: (value: number) => number; scale: number }): MiniRect {
  return { x: projection.x(node.position.x), y: projection.y(node.position.y), width: node.width * projection.scale, height: node.height * projection.scale }
}

export function minimapGeometry(nodes: MiniNode[], viewport: MiniViewport, canvas: { width: number; height: number }, size: { width: number; height: number }) {
  const zoom = Math.max(.01, viewport.zoom)
  const view = { x: -viewport.x / zoom, y: -viewport.y / zoom, width: canvas.width / zoom, height: canvas.height / zoom }
  const all = [...nodes.map((node) => ({ ...node.position, width: node.width, height: node.height })), view]
  const minX = Math.min(...all.map((item) => item.x)) - 100
  const minY = Math.min(...all.map((item) => item.y)) - 100
  const maxX = Math.max(...all.map((item) => item.x + item.width)) + 100
  const maxY = Math.max(...all.map((item) => item.y + item.height)) + 100
  const scale = Math.min((size.width - 20) / Math.max(1, maxX - minX), (size.height - 20) / Math.max(1, maxY - minY))
  const offsetX = (size.width - (maxX - minX) * scale) / 2
  const offsetY = (size.height - (maxY - minY) * scale) / 2
  const x = (flowX: number) => offsetX + (flowX - minX) * scale
  const y = (flowY: number) => offsetY + (flowY - minY) * scale
  const invert = (miniX: number, miniY: number) => ({ x: (miniX - offsetX) / scale + minX, y: (miniY - offsetY) / scale + minY })
  return {
    x, y, invert, scale,
    view: { x: x(view.x), y: y(view.y), width: view.width * scale, height: view.height * scale },
  }
}
