import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'

import { clearNotifications, type NotificationInput, notify } from '@/store/notifications'
import { stubResizeObserver } from '@/test/jsdom'

import { NotificationStack } from './notifications'

beforeAll(stubResizeObserver)

describe('toast titles', () => {
  beforeEach(() => {
    clearNotifications()
  })

  afterEach(() => {
    cleanup()
    clearNotifications()
  })

  it.each(['default', 'bottom-right'] as const)(
    'caps the %s toast stack at one back edge and keeps older notifications reachable',
    async placement => {
      for (let index = 0; index < 7; index++) {
        notify({ id: `notice-${index}`, message: `Notice ${index}`, placement, durationMs: 0 })
      }

      render(<NotificationStack />)
      expect(screen.getAllByRole('status')).toHaveLength(1)
      expect(document.querySelectorAll('[data-slot="card-stack-edge"]')).toHaveLength(1)
      fireEvent.click(screen.getByRole('button', { name: /Show.*6/ }))
      expect(screen.getByText('Notice 0')).toBeTruthy()
      expect(screen.getAllByRole('status')).toHaveLength(7)
      fireEvent.click(screen.getAllByRole('button', { name: /Dismiss/ })[0])
      await waitFor(() => expect(screen.queryByText('Notice 6')).toBeNull())
    }
  )
})

// Measured 2026-11-16 (chk-n5va.157): with the standing "Provider allowance" card on screen,
// restarting an MCP server produced no visible feedback for the toast's whole lifetime at all —
// the transient arrived behind a card that never leaves, so the front slot was never free.
// Both shapes of "never leaves on its own" are covered: a `persistent` standing offer and a
// sticky agent notice (`durationMs: 0`) such as the credits/allowance line.
describe('cards that never dismiss themselves', () => {
  type StandingShape = Omit<NotificationInput, 'id' | 'message' | 'placement'>

  const standing: [string, StandingShape][] = [
    ['a persistent standing offer', { durationMs: 0, kind: 'info', persistent: true }],
    ['a sticky agent notice', { durationMs: 0, kind: 'info' }]
  ]

  beforeEach(() => {
    clearNotifications()
  })

  afterEach(() => {
    cleanup()
    clearNotifications()
  })

  it.each(standing)('renders a transient toast in front of %s', async (_shape, offer) => {
    notify({ ...offer, id: 'standing', message: 'Provider allowance', placement: 'bottom-right' })

    render(<NotificationStack />)
    expect(screen.getByText('Provider allowance')).toBeTruthy()

    act(() => {
      notify({ durationMs: 0, id: 'restarted', kind: 'success', message: 'MCP server restarted' })
    })

    // The standing card does not keep the front slot: the new feedback is the one shown, and the
    // standing card folds behind it (reachable through "Show 1 more") instead of swallowing it.
    expect(screen.getByText('MCP server restarted')).toBeTruthy()
    await waitFor(() => expect(screen.queryByText('Provider allowance')).toBeNull())
  })

  it.each(standing)('hands the front slot back to %s once the transient departs', async (_shape, offer) => {
    notify({ ...offer, id: 'standing', message: 'Provider allowance', placement: 'bottom-right' })
    render(<NotificationStack />)

    act(() => {
      notify({ durationMs: 0, id: 'restarted', kind: 'success', message: 'MCP server restarted' })
    })
    expect(screen.getByText('MCP server restarted')).toBeTruthy()
    fireEvent.click(screen.getAllByRole('button', { name: /Dismiss/ })[0])

    await waitFor(() => expect(screen.queryByText('MCP server restarted')).toBeNull())
    expect(screen.getByText('Provider allowance')).toBeTruthy()
  })

  it('still lets a transient toast hold the front against another transient', async () => {
    notify({ id: 'first', kind: 'success', message: 'First saved' })
    render(<NotificationStack />)
    expect(screen.getByText('First saved')).toBeTruthy()

    act(() => {
      notify({ id: 'second', kind: 'success', message: 'Second saved' })
    })

    // Two cards that both dismiss themselves keep the old "stay on the one being read" rule:
    // the newer one is one click away behind the stack edge, not promoted.
    expect(screen.getByText('First saved')).toBeTruthy()
    expect(screen.queryByText('Second saved')).toBeNull()
  })
})
