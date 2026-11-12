import { describe, expect, it } from 'vitest'

import { adjacentTurnIndex, jumpToAdjacentTurn, turnLandingRow } from '../lib/turnJump.js'

// Six rows: three prompts, three replies, 6 rows apart.
const items = [
  { role: 'user' },
  { role: 'assistant' },
  { role: 'user' },
  { role: 'assistant' },
  { role: 'user' },
  { role: 'assistant' }
]

const rows = [0, 6, 12, 18, 24, 30]

describe('adjacentTurnIndex', () => {
  it('goes up to the nearest prompt above the viewport, down to the next one below', () => {
    expect(adjacentTurnIndex(items, rows, 20, -1)).toBe(2)
    expect(adjacentTurnIndex(items, rows, 20, 1)).toBe(4)
  })

  it('moves past a boundary the viewport already sits on instead of bouncing', () => {
    expect(adjacentTurnIndex(items, rows, 12, 1)).toBe(4)
    expect(adjacentTurnIndex(items, rows, 12, -1)).toBe(0)
  })

  it('reports no target at the ends', () => {
    expect(adjacentTurnIndex(items, rows, 0, -1)).toBe(-1)
    expect(adjacentTurnIndex(items, rows, 30, 1)).toBe(-1)
  })

  it('ignores rows that are not something the user typed', () => {
    const mixed = [
      { kind: 'slash', role: 'user' },
      { kind: 'event', role: 'system' },
      { role: 'user' },
      { role: 'assistant' }
    ]

    expect(adjacentTurnIndex(mixed, [0, 2, 4, 9], 8, -1)).toBe(2)
    expect(adjacentTurnIndex(mixed, [0, 2, 4, 9], 0, 1)).toBe(2)
  })

  it('is safe on empty or mismatched geometry', () => {
    expect(adjacentTurnIndex([], [], 10, 1)).toBe(-1)
    expect(adjacentTurnIndex(items, rows.slice(0, 2), 20, -1)).toBe(0)
    expect(adjacentTurnIndex(items, rows, Number.NaN, 1)).toBe(-1)
  })
})

describe('turnLandingRow', () => {
  it('leaves a two-row gap and never goes negative', () => {
    expect(turnLandingRow(12)).toBe(10)
    expect(turnLandingRow(1)).toBe(0)
  })
})

describe('jumpToAdjacentTurn', () => {
  const handle = (scrollTop: number, pending = 0) => {
    const calls: number[] = []
    let bottom = 0

    return {
      calls,
      get bottom() {
        return bottom
      },
      getPendingDelta: () => pending,
      getScrollTop: () => scrollTop,
      scrollTo: (y: number) => calls.push(y),
      scrollToBottom: () => {
        bottom++
      }
    }
  }

  it('scrolls to the previous prompt with the two-row gap', () => {
    const h = handle(20)

    expect(jumpToAdjacentTurn(h, items, rows, -1)).toBe(2)
    expect(h.calls).toEqual([10])
    expect(h.bottom).toBe(0)
  })

  it('accounts for a pending delta instead of the committed row', () => {
    const h = handle(20, -8) // a wheel tick already in flight: really at row 12

    expect(jumpToAdjacentTurn(h, items, rows, 1)).toBe(4)
    expect(h.calls).toEqual([22])
  })

  it('falls to the bottom below the last prompt and stays put above the first', () => {
    const last = handle(30)
    expect(jumpToAdjacentTurn(last, items, rows, 1)).toBe(-1)
    expect(last.calls).toEqual([])
    expect(last.bottom).toBe(1)

    const first = handle(0)
    expect(jumpToAdjacentTurn(first, items, rows, -1)).toBe(-1)
    expect(first.calls).toEqual([])
    expect(first.bottom).toBe(0)
  })
})
