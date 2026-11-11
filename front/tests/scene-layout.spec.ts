import { describe, expect, it } from 'vitest'
import { layoutSceneBranches, SHOTS_PER_ROW, SHOT_ROW_GAP, SCENE_GAP } from '../src/sceneLayout'

describe('branched scene layout', () => {
  it('uses visible card dimensions without reserving a prompt lane in every row', () => {
    const result = layoutSceneBranches({ x: 100, y: 100 }, [
      { id: 'a', open: true, shotHeights: Array(6).fill(180), shotWidths: Array(6).fill(180) },
      { id: 'b', open: true, shotHeights: [180], shotWidths: [180] },
    ])
    expect(result.a.shots[3].y - result.a.shots[0].y).toBe(180 + SHOT_ROW_GAP)
    expect(result.a.shots[1].x - result.a.shots[0].x).toBe(216)
    expect(result.a.bounds.height).toBe(400)
    expect(result.b.scene.y - result.a.scene.y).toBe(400 + SCENE_GAP)
  })
  it('uses the tallest card in each row for mixed image aspect ratios', () => {
    const heights = [180, 300, 210, 240, 160, 180, 200]
    const { a } = layoutSceneBranches({ x: 0, y: 0 }, [{ id: 'a', open: true, shotHeights: heights }])
    expect(a.shots[3].y - a.shots[0].y).toBe(300 + SHOT_ROW_GAP)
    expect(a.shots[6].y - a.shots[3].y).toBe(240 + SHOT_ROW_GAP)
    for (let index = 0; index < 6; index++) expect(a.shots[Math.floor(index / 3) * 3 + 3].y).toBeGreaterThan(a.shots[index].y + heights[index])
  })
  it('fans scenes out and starts a second branch column after six', () => {
    const scenes = Array.from({ length: 8 }, (_, index) => ({ id: `s${index}`, open: false, shotHeights: [] }))
    const result = layoutSceneBranches({ x: 100, y: 100 }, scenes)
    expect(new Set(Object.values(result).map((item) => `${item.scene.x},${item.scene.y}`)).size).toBe(8)
    expect(result.s6.scene.x).toBeGreaterThan(result.s0.scene.x)
    expect(result.s0.scene.y).toBeLessThan(result.s5.scene.y)
  })

  it('lays out every one of 53 shots without pagination or one long row', () => {
    const result = layoutSceneBranches({ x: 0, y: 0 }, [{ id: 'long', open: true, shotHeights: Array(53).fill(310) }])
    expect(result.long.shots).toHaveLength(53)
    expect(new Set(result.long.shots.map((shot) => shot.y)).size).toBe(Math.ceil(53 / SHOTS_PER_ROW))
    expect(new Set(result.long.shots.slice(0, SHOTS_PER_ROW).map((shot) => shot.y)).size).toBe(1)
  })

  it('keeps 1, 10, 11, 30, and 53 shots addressable without overlap', () => {
    for (const count of [1, 10, 11, 30, 53]) {
      const result = layoutSceneBranches({ x: 0, y: 0 }, [{ id: 'scene', open: true, shotHeights: Array(count).fill(290) }])
      const positions = result.scene.shots.map((shot) => `${shot.x}:${shot.y}`)
      expect(positions).toHaveLength(count)
      expect(new Set(positions).size).toBe(count)
      expect(new Set(result.scene.shots.map((shot) => shot.y)).size).toBe(Math.ceil(count / SHOTS_PER_ROW))
    }
  })

  it('retains 53 logical shot positions under a collapsed scene', () => {
    const result = layoutSceneBranches({ x: 0, y: 0 }, [{ id: 'collapsed', open: false, shotHeights: Array(53).fill(320) }])
    expect(result.collapsed.shots).toHaveLength(53)
    expect(result.collapsed.bounds.width).toBe(250)
    expect(result.collapsed.shots.every((shot) => shot.x < result.collapsed.scene.x + 250)).toBe(true)
  })

  it('moves only the colliding branch to the right of occupied space', () => {
    const scenes = [{ id: 'a', open: true, shotHeights: Array(11).fill(310) }, { id: 'b', open: false, shotHeights: [] }]
    const result = layoutSceneBranches({ x: 0, y: 0 }, scenes, [{ x: 380, y: -160, width: 260, height: 260 }])
    expect(result.a.scene.x).toBeGreaterThan(640)
    expect(result.b.scene.x).toBeLessThan(result.a.bounds.x + result.a.bounds.width)
    expect(result.b.bounds.y).toBeGreaterThan(result.a.bounds.y)
  })
  it('packs later scenes below an expanded subtree instead of pushing them into a long horizontal branch', () => {
    const result = layoutSceneBranches({ x: 100, y: 100 }, [
      { id: 'a', open: true, shotHeights: Array(8).fill(340) },
      { id: 'b', open: true, shotHeights: Array(3).fill(280) },
      { id: 'c', open: false, shotHeights: [] },
    ])
    expect(Math.abs(result.b.scene.x - result.a.scene.x)).toBeLessThan(100)
    expect(result.b.scene.y).toBeGreaterThan(result.a.bounds.y + result.a.bounds.height)
    expect(result.c.scene.y).toBeGreaterThan(result.b.bounds.y + result.b.bounds.height)
    expect(new Set(result.a.shots.slice(0, SHOTS_PER_ROW).map(position => position.y)).size).toBe(1)
  })
  it('places second branch columns beyond expanded first-column cards without overlap', () => {
    const result = layoutSceneBranches({ x: 0, y: 0 }, Array.from({ length: 8 }, (_, index) => ({
      id: `s${index}`, open: index === 0, shotHeights: Array(11).fill(360),
    })))
    expect(result.s6.scene.x).toBeGreaterThan(result.s0.bounds.x + result.s0.bounds.width)
    for (const [index, a] of Object.values(result).entries()) for (const b of Object.values(result).slice(index + 1)) {
      expect(a.bounds.x < b.bounds.x + b.bounds.width && a.bounds.x + a.bounds.width > b.bounds.x && a.bounds.y < b.bounds.y + b.bounds.height && a.bounds.y + a.bounds.height > b.bounds.y).toBe(false)
    }
  })
  it('unfolds six cards as two compact rows without moving the cover away from its input', () => {
    const anchor = { x: 100, y: 100 }
    const scenes = [{ id: 'a', open: false, shotHeights: Array(6).fill(360) }]
    const collapsed = layoutSceneBranches(anchor, scenes).a
    const expanded = layoutSceneBranches(anchor, [{ ...scenes[0], open: true }]).a
    expect(expanded.scene).toEqual(collapsed.scene)
    expect(expanded.scene.x - anchor.x).toBeLessThan(500)
    expect(expanded.bounds.width).toBeLessThan(1200)
    expect(new Set(expanded.shots.map(shot => shot.y)).size).toBe(2)
  })
  it('moves only the unfolded grid around an obstacle to the right of the cover', () => {
    const scenes = [{ id: 'a', open: false, shotHeights: Array(6).fill(360) }]
    const obstacle = { x: 1100, y: 0, width: 268, height: 320 }
    const collapsed = layoutSceneBranches({ x: 100, y: 100 }, scenes, [obstacle]).a
    const expanded = layoutSceneBranches({ x: 100, y: 100 }, [{ ...scenes[0], open: true }], [obstacle]).a
    expect(expanded.scene).toEqual(collapsed.scene)
    expect(expanded.shots[0].x - expanded.scene.x).toBe(320)
    expect(expanded.shots.every(shot => shot.y > obstacle.y + obstacle.height)).toBe(true)
  })
})
