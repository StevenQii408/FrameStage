/** Self-authored Canvas 2D effect. Canvas starfield projection is a visual reference
 * from TheodorStorm/starfield (MIT); no upstream code or runtime is included. */
export const STARFIELD_LIMITS = Object.freeze({ fps: 30, perCard: 48, total: 320, dpr: 1.5 })

export interface StarfieldFrame {
  delta: number
  now: number
  particles: number
  updateClock: boolean
}

interface ClockEnvironment {
  request: (callback: FrameRequestCallback) => number
  cancel: (id: number) => void
  now: () => number
  hidden: () => boolean
  listen: (callback: () => void) => () => void
  delay?: (callback: () => void, milliseconds: number) => number
  clearDelay?: (id: number) => void
}

/** All generation cards share one animation frame and one visibility listener. */
export function createStarfieldScheduler(environment: ClockEnvironment) {
  const entries = new Map<number, { draw: (frame: StarfieldFrame) => void; paused: boolean; clockWhenPaused: boolean; clock: number }>()
  let sequence = 0
  let pending: number | null = null
  let previous: number | null = null
  let clockTimer: number | null = null
  let unlisten: (() => void) | undefined
  const active = () => [...entries.values()].filter(entry => !entry.paused)
  const stopFrames = () => {
    if (pending != null) environment.cancel(pending)
    pending = null
    previous = null
  }
  const stopClock = () => {
    if (clockTimer != null) environment.clearDelay?.(clockTimer)
    clockTimer = null
  }
  const updatePausedClocks = (now: number) => {
    for (const entry of entries.values()) {
      if (entry.paused && entry.clockWhenPaused && (!entry.clock || now < entry.clock || now - entry.clock >= 1000)) {
        entry.clock = now
        entry.draw({ delta: 0, now, particles: 0, updateClock: true })
      }
    }
  }
  const schedule = () => {
    if (environment.hidden() || !entries.size) { stopFrames(); stopClock(); return }
    if (active().length) {
      stopClock()
      if (pending == null) pending = environment.request(tick)
    } else {
      stopFrames()
      if ([...entries.values()].some(entry => entry.clockWhenPaused) && environment.delay && clockTimer == null) {
        clockTimer = environment.delay(() => {
          clockTimer = null
          if (!environment.hidden()) updatePausedClocks(environment.now())
          schedule()
        }, 1000)
      } else if (![...entries.values()].some(entry => entry.clockWhenPaused)) stopClock()
    }
  }
  const tick = (timestamp: number) => {
    pending = null
    const surfaces = active()
    if (environment.hidden() || !surfaces.length) { schedule(); return }
    const elapsed = previous == null ? 1000 / STARFIELD_LIMITS.fps : timestamp - previous
    if (elapsed >= 1000 / STARFIELD_LIMITS.fps - .5) {
      previous = timestamp
      const now = environment.now()
      const particles = Math.min(STARFIELD_LIMITS.perCard, Math.floor(STARFIELD_LIMITS.total / surfaces.length))
      for (const surface of surfaces) {
        const updateClock = !surface.clock || now < surface.clock || now - surface.clock >= 1000
        if (updateClock) surface.clock = now
        surface.draw({ delta: Math.min(elapsed, 50) / 1000, now, particles, updateClock })
      }
      updatePausedClocks(now)
    }
    schedule()
  }
  return {
    subscribe(draw: (frame: StarfieldFrame) => void, paused = false, clockWhenPaused = false) {
      const id = ++sequence
      entries.set(id, { draw, paused, clockWhenPaused, clock: 0 })
      if (!unlisten) unlisten = environment.listen(schedule)
      schedule()
      return {
        setPaused(value: boolean, keepClock = false) {
          const entry = entries.get(id)
          if (!entry || (entry.paused === value && entry.clockWhenPaused === keepClock)) return
          entry.paused = value
          entry.clockWhenPaused = keepClock
          entry.clock = 0
          schedule()
        },
        destroy() {
          entries.delete(id)
          if (!entries.size) { stopFrames(); stopClock(); unlisten?.(); unlisten = undefined }
          else schedule()
        },
      }
    },
  }
}

let sharedScheduler: ReturnType<typeof createStarfieldScheduler> | undefined
const scheduler = () => sharedScheduler ??= createStarfieldScheduler({
  request: callback => window.requestAnimationFrame(callback),
  cancel: id => window.cancelAnimationFrame(id),
  now: () => Date.now(),
  hidden: () => document.hidden,
  delay: (callback, milliseconds) => window.setTimeout(callback, milliseconds),
  clearDelay: id => window.clearTimeout(id),
  listen: callback => {
    document.addEventListener('visibilitychange', callback)
    return () => document.removeEventListener('visibilitychange', callback)
  },
})

interface Star { x: number; y: number; depth: number; size: number; phase: number; color: number }
interface Comet { x: number; y: number; age: number }
interface SurfaceOptions { kind: 'image' | 'video'; paused?: boolean; clockWhenPaused?: boolean; level?: number | null; onClock: (now: number) => void }

export interface GenerationTideGeometry { level: number | null; surfaceY: number; amplitude: number; fillFraction: number }

/** Exact height mapping, including empty/full endpoints. Unknown is a fixed low
 * tide cue, not synthetic progress. Animation only offsets this bounded surface. */
export function generationTideGeometry(height: number, level?: number | null): GenerationTideGeometry {
  const safeHeight = Number.isFinite(height) ? Math.max(0, height) : 0
  const known = typeof level === 'number' && Number.isFinite(level) ? Math.max(0, Math.min(100, level)) : null
  const fillFraction = known === null ? .1 : known / 100
  const surfaceY = safeHeight * (1 - fillFraction)
  return { level: known, fillFraction, surfaceY,
    amplitude: Math.max(0, Math.min(4.2, safeHeight * .025, surfaceY * .4, (safeHeight - surfaceY) * .4)) }
}

/** Both sides of the wave remain inside the frame, even at 0% and 100%. */
export function generationTideSurface(xFraction: number, tide: GenerationTideGeometry, time: number, layer = 0): number {
  const phase = xFraction * Math.PI * 2
  const wave = layer === 0 ? Math.sin(phase + time * .72) * .72 + Math.sin(phase * 2 - time * .4) * .22
    : Math.sin(phase * 1.3 - time * .53 + 1.8) * .65 - .25
  return tide.surfaceY + tide.amplitude * wave
}

function context2d(canvas: HTMLCanvasElement) {
  // Native canvas can be unavailable in nonvisual documents. Keep the status readable.
  if (typeof CanvasRenderingContext2D === 'undefined') return null
  try { return canvas.getContext('2d') } catch { return null }
}

const sprites = new Map<string, HTMLCanvasElement>()
const palettes = { image: ['#78ddff', '#b599ff'], video: ['#a8a1ff', '#6ed7ff'] } as const
let surfaceSequence = 0
function glowSprite(color: string) {
  const cached = sprites.get(color)
  if (cached) return cached
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 32
  const context = context2d(canvas)
  if (!context) return null
  const glow = context.createRadialGradient(16, 16, 0, 16, 16, 16)
  glow.addColorStop(0, '#f0fbff')
  glow.addColorStop(.1, color)
  glow.addColorStop(.3, color + '70')
  glow.addColorStop(1, color + '00')
  context.fillStyle = glow
  context.fillRect(0, 0, 32, 32)
  sprites.set(color, canvas)
  return canvas
}

/** Depth projection and static cached glow sprites keep each frame inexpensive. */
export function mountGenerationStarfield(canvas: HTMLCanvasElement, options: SurfaceOptions) {
  const context = context2d(canvas)
  let kind = options.kind
  let paused = Boolean(options.paused)
  let width = 268
  let height = 150
  let elapsed = 0
  let targetLevel = generationTideGeometry(height, options.level).level
  let visibleLevel = targetLevel
  let lastBudget: number = STARFIELD_LIMITS.perCard
  let nextComet = 5.5
  let comet: Comet | null = null
  // A deterministic local generator keeps re-renders from replacing the star layout.
  let seed = (0x4f17b2 ^ Math.imul(++surfaceSequence, 2654435761)) >>> 0
  const random = () => { seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0; return seed / 4294967296 }
  const stars: Star[] = Array.from({ length: STARFIELD_LIMITS.perCard }, () => ({
    x: random() * 1.5 - .75, y: random() * 1.5 - .75, depth: .25 + random() * 1.3,
    size: .3 + random() * .65, phase: random() * Math.PI * 2, color: random() < .64 ? 0 : 1,
  }))
  nextComet += random() * 4
  let seaGradient: CanvasGradient | null = null
  let seaGlow: CanvasGradient | null = null
  function wavePath(tide: GenerationTideGeometry, layer: number) {
    if (!context) return
    context.beginPath()
    for (let x = 0; x < width; x += 8) {
      const y = generationTideSurface(x / width, tide, elapsed, layer)
      if (x === 0) context.moveTo(x, y)
      else context.lineTo(x, y)
    }
    context.lineTo(width, generationTideSurface(1, tide, elapsed, layer))
  }
  function fillSea(tide: GenerationTideGeometry) {
    if (!context || tide.fillFraction <= 0) return
    for (const layer of [1, 0]) {
      context.save()
      wavePath(tide, layer)
      context.lineTo(width, height)
      context.lineTo(0, height)
      context.closePath()
      context.clip()
      context.globalAlpha = layer === 1 ? .35 : .82
      context.fillStyle = seaGradient || '#214466'
      context.fillRect(0, 0, width, height)
      if (seaGlow) { context.fillStyle = seaGlow; context.fillRect(0, 0, width, height) }
      context.restore()
      if (tide.level !== 0 && tide.level !== 100) {
        wavePath(tide, layer)
        context.strokeStyle = palettes[kind][layer]
        context.globalAlpha = layer === 0 ? .67 : .28
        context.lineWidth = layer === 0 ? 1 : .7
        context.stroke()
      }
    }
    context.globalAlpha = 1
  }
  const draw = (delta: number, budget: number) => {
    if (!context || !width || !height) return
    elapsed += delta
    lastBudget = budget
    if (delta > 0 && visibleLevel !== null && targetLevel !== null) {
      const difference = targetLevel - visibleLevel
      visibleLevel = Math.abs(difference) < .02 ? targetLevel : visibleLevel + difference * Math.min(1, delta * 5)
    }
    context.clearRect(0, 0, width, height)
    const tide = generationTideGeometry(height, visibleLevel)
    fillSea(tide)
    if (budget <= 0) return
    const colors = palettes[kind]
    const glow = [glowSprite(colors[0]), glowSprite(colors[1])]
    if (!comet && elapsed > nextComet && budget > 8 && delta > 0) {
      comet = { x: .15 + random() * .45, y: random() * .3, age: 0 }
      nextComet = elapsed + 7 + random() * 6
    }
    const dustCount = tide.level !== null && tide.level > 0 && tide.level < 100 && budget > 12 ? 4 : 0
    const count = Math.max(0, budget - (comet ? 1 : 0) - dustCount)
    for (let index = 0; index < count; index++) {
      const star = stars[index]!
      star.depth -= delta * .046
      if (star.depth < .12) { star.depth = 1.55; star.x = random() * 1.5 - .75; star.y = random() * 1.5 - .75 }
      const projection = .58 / (.3 + star.depth)
      const x = width * (.5 + star.x * projection)
      const y = height * (.5 + star.y * projection)
      if (x < -12 || y < -12 || x > width + 12 || y > height + 12) continue
      const submerged = y >= generationTideSurface(x / width, tide, elapsed)
      if (!submerged && index % 4 !== 0) continue
      const sprite = glow[star.color]
      if (!sprite) continue
      const radius = (star.size + .35 / star.depth) * 3
      context.globalAlpha = Math.min(.92, .26 + .32 / (.5 + star.depth) + Math.sin(elapsed * .7 + star.phase) * .12) * (submerged ? 1 : .24)
      context.drawImage(sprite, x - radius, y - radius, radius * 2, radius * 2)
    }
    // A few looping specks detach from the crest; they share the particle cap.
    for (let index = 0; index < dustCount; index++) {
      const star = stars[index]!
      const age = (elapsed * .26 + star.phase / (Math.PI * 2)) % 1
      const x = width * ((index / dustCount + .12 + Math.sin(elapsed * .08 + star.phase) * .035) % 1)
      const y = generationTideSurface(x / width, tide, elapsed) - age * Math.min(18, height * .12)
      const sprite = glow[index % 2]
      if (!sprite) continue
      context.globalAlpha = Math.sin(age * Math.PI) * .55
      context.drawImage(sprite, x - 2, y - 2, 4, 4)
    }
    if (comet) {
      comet.age += delta
      if (comet.age > 1.1) comet = null
      else {
        const x = (comet.x + comet.age * .48) * width
        const y = (comet.y + comet.age * .35) * height
        const submerged = y >= generationTideSurface(x / width, tide, elapsed)
        context.globalAlpha = Math.sin(comet.age / 1.1 * Math.PI) * (submerged ? .7 : .16)
        context.strokeStyle = colors[0]!
        context.lineWidth = 1
        context.beginPath()
        context.moveTo(x - width * .1, y - height * .073)
        context.lineTo(x, y)
        context.stroke()
        const sprite = glow[0]
        if (sprite) context.drawImage(sprite, x - 5, y - 5, 10, 10)
      }
    }
    context.globalAlpha = 1
  }
  const resize = (newWidth: number, newHeight: number) => {
    width = Math.max(1, Math.min(2048, newWidth))
    height = Math.max(1, Math.min(2048, newHeight))
    const dpr = Math.min(STARFIELD_LIMITS.dpr, window.devicePixelRatio || 1)
    canvas.width = Math.round(width * dpr)
    canvas.height = Math.round(height * dpr)
    context?.setTransform(dpr, 0, 0, dpr, 0, 0)
    if (context) {
      seaGradient = context.createLinearGradient(0, 0, width * .2, height)
      seaGradient.addColorStop(0, kind === 'video' ? '#5c4b9cc0' : '#276e96c9')
      seaGradient.addColorStop(.58, '#263e77d1')
      seaGradient.addColorStop(1, '#101b3ce0')
      seaGlow = context.createRadialGradient(width * .72, height * .82, 0, width * .6, height, width * .8)
      seaGlow.addColorStop(0, kind === 'video' ? '#8083d64f' : '#8566ce58')
      seaGlow.addColorStop(1, '#6f4a9800')
    }
    draw(0, lastBudget)
  }
  resize(canvas.clientWidth || width, canvas.clientHeight || height)
  const observer = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(entries => {
    const size = entries[0]?.contentRect
    if (size) resize(size.width, size.height)
  }) : null
  observer?.observe(canvas)
  const animation = scheduler().subscribe(frame => {
    if (frame.updateClock) options.onClock(frame.now)
    if (!paused) draw(frame.delta, frame.particles)
  }, paused, options.clockWhenPaused)
  return {
    setPaused(value: boolean, keepClock = false) {
      paused = value
      animation.setPaused(paused, keepClock)
      if (paused) { visibleLevel = targetLevel; draw(0, lastBudget) }
    },
    setKind(value: 'image' | 'video') { kind = value; resize(width, height) },
    setLevel(value: number | null) {
      targetLevel = generationTideGeometry(height, value).level
      if (paused || targetLevel === null || visibleLevel === null) {
        visibleLevel = targetLevel
        draw(0, lastBudget)
      }
    },
    destroy() {
      animation.destroy()
      observer?.disconnect()
      context?.clearRect(0, 0, width, height)
      comet = null
    },
  }
}
