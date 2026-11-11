export type Point = { x: number; y: number }

export function energyControls(source: Point, target: Point): [Point, Point] {
  const bend = Math.max(60, Math.min(220, (target.x - source.x) * .48))
  return [{ x: source.x + bend, y: source.y }, { x: target.x - bend, y: target.y }]
}

export function energyPath(source: Point, target: Point): string {
  const [a, b] = energyControls(source, target)
  return `M ${source.x} ${source.y} C ${a.x} ${a.y}, ${b.x} ${b.y}, ${target.x} ${target.y}`
}

/** Carry time order through the gutter, not diagonally across thumbnail faces. */
export function timelineReturnPath(source: Point, target: Point, rowBottom: number): string {
  if (!Number.isFinite(rowBottom) || target.y - source.y < 32) return energyPath(source, target)
  const lane = Math.max(source.y + 16, Math.min(rowBottom, target.y - 16))
  const right = source.x + 24, left = target.x - 24, radius = 8
  return `M ${source.x} ${source.y} H ${right - radius} Q ${right} ${source.y} ${right} ${source.y + radius} V ${lane - radius} Q ${right} ${lane} ${right - radius} ${lane} H ${left + radius} Q ${left} ${lane} ${left} ${lane + radius} V ${target.y - radius} Q ${left} ${target.y} ${left + radius} ${target.y} H ${target.x}`
}

export function energyPoint(source: Point, target: Point, t: number): Point {
  const [a, b] = energyControls(source, target)
  const u = 1 - Math.max(0, Math.min(1, t))
  const v = 1 - u
  return {
    x: u * u * u * source.x + 3 * u * u * v * a.x + 3 * u * v * v * b.x + v * v * v * target.x,
    y: u * u * u * source.y + 3 * u * u * v * a.y + 3 * u * v * v * b.y + v * v * v * target.y,
  }
}
