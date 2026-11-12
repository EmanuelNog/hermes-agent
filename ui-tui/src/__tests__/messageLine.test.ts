import { describe, expect, it } from 'vitest'

import {
  isCollapsibleSystemMessage,
  shouldShowResponseSeparator,
  shouldShowThinkingTrail
} from '../components/messageLine.js'

describe('isCollapsibleSystemMessage', () => {
  it('collapses a long dump the user did not ask to read (system prompt, AGENTS.md)', () => {
    expect(isCollapsibleSystemMessage({ role: 'system', text: 'x'.repeat(700) })).toBe(true)
    expect(isCollapsibleSystemMessage({ role: 'system', text: 'short' })).toBe(false)
    expect(isCollapsibleSystemMessage({ role: 'assistant', text: 'x'.repeat(700) })).toBe(false)
  })

  it('never collapses a recall hint: the crops ARE the hint', () => {
    const hint = '💡 Prior Context Hint · 5 shown of 12 in 56ms\n  ' + 'crop row\n  '.repeat(60)

    expect(isCollapsibleSystemMessage({ role: 'system', kind: 'recall', text: hint })).toBe(false)
  })
})

describe('shouldShowResponseSeparator', () => {
  it('separates assistant response text from visible details', () => {
    expect(shouldShowResponseSeparator({ role: 'assistant', text: 'final', thinking: 'plan' }, true)).toBe(true)
  })

  it('does not add a response separator without details or body text', () => {
    expect(shouldShowResponseSeparator({ role: 'assistant', text: 'final' }, false)).toBe(false)
    expect(shouldShowResponseSeparator({ role: 'assistant', text: '   ', thinking: 'plan' }, true)).toBe(false)
  })

  it('does not add response separators to non-assistant transcript rows', () => {
    expect(shouldShowResponseSeparator({ role: 'user', text: 'prompt' }, true)).toBe(false)
    expect(shouldShowResponseSeparator({ role: 'system', text: 'note' }, true)).toBe(false)
  })
})

describe('shouldShowThinkingTrail', () => {
  it('hides an ordinary reasoning trail when every section is hidden', () => {
    const msg = { role: 'system', text: '', thinking: 'plan' } as const
    expect(shouldShowThinkingTrail(msg, 'hidden', 'hidden', 'hidden')).toBe(false)
  })

  it('shows an ordinary reasoning trail when any section is visible', () => {
    const msg = { role: 'system', text: '', thinking: 'plan' } as const
    expect(shouldShowThinkingTrail(msg, 'collapsed', 'hidden', 'hidden')).toBe(true)
    expect(shouldShowThinkingTrail(msg, 'hidden', 'collapsed', 'hidden')).toBe(true)
    expect(shouldShowThinkingTrail(msg, 'hidden', 'hidden', 'expanded')).toBe(true)
  })

  it('keeps a MoA reference block visible even when every section is hidden (#64657)', () => {
    const msg = {
      role: 'system',
      text: '',
      thinking: '◇ Reference 1/2 — model-a\nadvice-a',
      isMoaReference: true
    } as const

    expect(shouldShowThinkingTrail(msg, 'hidden', 'hidden', 'hidden')).toBe(true)
  })
})
