import { describe, expect, it } from 'vitest'
import { generatorPanelAnchor } from '../src/generatorPanelGeometry'
describe('generator editor positioning', () => {
  it('opens below the node and clamps to a usable screen area without changing zoom', () => {
    const node = { left: 600, top: 100, width: 268, height: 230 }
    const normal = generatorPanelAnchor(node, { width: 1440, height: 900 })
    expect(normal.width).toBe(720)
    expect(normal.y).toBe(344)
    expect(normal.x + normal.width / 2).toBe(node.left + node.width / 2)
    const corner = generatorPanelAnchor({ ...node, left: 1380, top: 800 }, { width: 1440, height: 900 })
    expect(corner.x + corner.width).toBeLessThanOrEqual(1428)
    expect(corner.y).toBeLessThanOrEqual(675)
  })
  it('keeps the composer inside the screen when the node is near either side', () => {
    for (const left of [-80, 0, 980]) {
      const panel = generatorPanelAnchor({ left, top: 80, width: 268, height: 230 }, { width: 1120, height: 820 })
      expect(panel.x).toBeGreaterThanOrEqual(12)
      expect(panel.x + panel.width).toBeLessThanOrEqual(1108)
    }
  })
  it('does not cover the generation readout when a compact composer fits below', () => {
    const node = { left: 500, top: 650, width: 268, height: 150 }
    const panel = generatorPanelAnchor(node, { width: 1600, height: 1050 })
    expect(panel.y).toBeGreaterThan(node.top + node.height)
    const bottom = generatorPanelAnchor({ ...node, top: 850 }, { width: 1600, height: 1050 })
    expect(bottom.y + 330).toBeLessThan(850)
  })
})
