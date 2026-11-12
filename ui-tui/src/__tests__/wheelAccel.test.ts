import { afterEach, describe, expect, it } from 'vitest'

import {
  computeWheelStep,
  currentScrollSpeedBase,
  initWheelAccel,
  setScrollSpeedBase,
  syncScrollSpeedBase
} from '../lib/wheelAccel.js'

describe('wheelAccel — native path', () => {
  it('first click after init returns base', () => {
    const s = initWheelAccel(false, 1)

    expect(computeWheelStep(s, 1, 1000)).toBe(1)
  })

  it('gap beyond window resets mult to base', () => {
    const s = initWheelAccel(false, 1)

    for (let t = 1000; t < 1100; t += 20) {
      computeWheelStep(s, 1, t)
    }

    expect(computeWheelStep(s, 1, 2000)).toBe(1)
  })

  it('direction flip defers one event for bounce detection', () => {
    const s = initWheelAccel(false, 1)

    computeWheelStep(s, 1, 1000)

    expect(computeWheelStep(s, -1, 1050)).toBe(0)
  })

  it('flip-back within bounce window engages wheelMode', () => {
    const s = initWheelAccel(false, 1)

    computeWheelStep(s, 1, 1000)
    computeWheelStep(s, -1, 1050)
    computeWheelStep(s, 1, 1100)

    expect(s.wheelMode).toBe(true)
  })

  it('flip-back outside bounce window is a real reversal (no wheelMode)', () => {
    const s = initWheelAccel(false, 1)

    computeWheelStep(s, 1, 1000)
    computeWheelStep(s, -1, 1050)
    computeWheelStep(s, 1, 1400)

    expect(s.wheelMode).toBe(false)
  })

  it('5 consecutive sub-5ms events disengage wheelMode (trackpad signature)', () => {
    const s = initWheelAccel(false, 1)
    s.wheelMode = true
    s.dir = 1
    s.time = 1000

    for (let t = 1002; t <= 1010; t += 2) {
      computeWheelStep(s, 1, t)
    }

    expect(s.wheelMode).toBe(false)
  })

  it('1.5s idle disengages wheelMode', () => {
    const s = initWheelAccel(false, 1)
    s.wheelMode = true
    s.dir = 1
    s.time = 1000

    computeWheelStep(s, 1, 3000)

    expect(s.wheelMode).toBe(false)
  })
})

describe('wheelAccel — scroll speed knobs', () => {
  afterEach(() => {
    // Module-level configured base is shared state: never leak into another file.
    setScrollSpeedBase(null)
    delete process.env.HERMES_TUI_SCROLL_SPEED
    delete process.env.CLAUDE_CODE_SCROLL_SPEED
  })

  it('display.tui_scroll_speed raises rows per notch', () => {
    const s = initWheelAccel(false, 1)

    setScrollSpeedBase(3)
    syncScrollSpeedBase(s)

    expect(computeWheelStep(s, 1, 1000)).toBe(3)
  })

  it('an env override still wins over the config value', () => {
    process.env.HERMES_TUI_SCROLL_SPEED = '7'
    setScrollSpeedBase(3)

    expect(currentScrollSpeedBase()).toBe(7)
  })

  it('invalid and out-of-range values are ignored or clamped', () => {
    setScrollSpeedBase('fast')

    expect(currentScrollSpeedBase()).toBe(1)

    setScrollSpeedBase(0)

    expect(currentScrollSpeedBase()).toBe(1)

    setScrollSpeedBase(999)

    expect(currentScrollSpeedBase()).toBe(20)
  })

  it('a live change reaches a state that already scrolled', () => {
    const s = initWheelAccel(false, 1)

    computeWheelStep(s, 1, 1000)
    setScrollSpeedBase(4)
    syncScrollSpeedBase(s)

    // Same direction, 20ms apart: accel ramps on top of the new floor, never below it.
    expect(computeWheelStep(s, 1, 1010)).toBeGreaterThanOrEqual(4)
  })
})

describe('wheelAccel — xterm.js path', () => {
  it('sub-5ms burst returns 1 (same-direction, same-batch)', () => {
    const s = initWheelAccel(true, 1)

    computeWheelStep(s, 1, 1000)

    expect(computeWheelStep(s, 1, 1002)).toBe(1)
  })

  it('slow steady scroll stays in precision range', () => {
    const s = initWheelAccel(true, 1)

    for (let t = 1000; t < 2000; t += 33) {
      const r = computeWheelStep(s, 1, t)

      expect(r).toBeGreaterThanOrEqual(1)
      expect(r).toBeLessThanOrEqual(6)
    }
  })

  it('direction reversal resets mult', () => {
    const s = initWheelAccel(true, 1)

    for (let t = 1000; t < 1100; t += 20) {
      computeWheelStep(s, 1, t)
    }

    const beforeFlip = s.mult

    computeWheelStep(s, -1, 1200)

    expect(s.mult).toBeLessThanOrEqual(beforeFlip)
    expect(s.mult).toBe(2)
  })

  it('frac stays in [0,1) across events', () => {
    const s = initWheelAccel(true, 1)

    // Correctness invariant of fractional carry: never negative, never reaches 1.
    for (let t = 1000; t < 1200; t += 30) {
      computeWheelStep(s, 1, t)

      expect(s.frac).toBeGreaterThanOrEqual(0)
      expect(s.frac).toBeLessThan(1)
    }
  })
})
