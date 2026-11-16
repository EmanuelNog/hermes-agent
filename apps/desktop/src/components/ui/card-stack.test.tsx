import { cleanup, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { stubResizeObserver } from '@/test/jsdom'

import { CardStack } from './card-stack'

function Stack({ items, pinned }: { items: string[]; pinned?: (item: string) => boolean }) {
  return (
    <CardStack getKey={item => item} items={items} pinned={pinned} surfaceClassName="border">
      {(item, action) => <button disabled={!action.active || action.busy}>{item}</button>}
    </CardStack>
  )
}

beforeEach(() => {
  stubResizeObserver()
  vi.spyOn(HTMLElement.prototype, 'offsetHeight', 'get').mockReturnValue(96)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('CardStack completion footprint', () => {
  it('retires the footprint while the final card departs, not after it disappears', () => {
    const { container, rerender } = render(<Stack items={['first']} />)
    const host = container.querySelector<HTMLElement>('[data-slot="card-stack"]')!
    expect(parseFloat(host.style.minHeight)).toBeGreaterThan(0)

    rerender(<Stack items={[]} />)

    // The painted exit can continue, but it cannot leave its full layout
    // height behind until the last frame and make a bottom-locked chat snap.
    expect(host.querySelector('[data-stack-key]')).not.toBeNull()
    expect(host.querySelector('[data-stack-key]')?.hasAttribute('inert')).toBe(true)
    expect(host.style.minHeight).toBe('0px')
    expect(host.style.transition).toContain('min-height')
  })

  it('keeps the next live card measured when a previous departure finishes', async () => {
    const { container, rerender, getByRole } = render(<Stack items={['first']} />)
    const host = container.querySelector<HTMLElement>('[data-slot="card-stack"]')!
    const occupiedHeight = host.style.minHeight
    rerender(<Stack items={[]} />)
    rerender(<Stack items={['next']} />)

    await waitFor(() => expect(container.querySelector('[data-stack-key="first"]')).toBeNull())

    expect(container.querySelector('[data-slot="card-stack"]')).toBe(host)
    expect((getByRole('button', { name: 'next' }) as HTMLButtonElement).disabled).toBe(false)
    expect(host.style.minHeight).toBe(occupiedHeight)
    expect(host.style.transition).toBe('')
  })

  // A pinned card (one that never departs on its own) is visible from anywhere in the stack, so
  // holding the front for it would hide every newer card behind it for as long as it stands.
  it('yields the front slot when the held card is pinned', async () => {
    const pinned = (item: string) => item === 'standing'
    const { container, rerender } = render(<Stack items={['standing']} pinned={pinned} />)
    expect(container.querySelector('[data-stack-key="standing"]')?.getAttribute('data-stack-active')).toBe('true')

    rerender(<Stack items={['fresh', 'standing']} pinned={pinned} />)

    await waitFor(() =>
      expect(container.querySelector('[data-stack-key="fresh"]')?.getAttribute('data-stack-active')).toBe('true')
    )
    await waitFor(() => expect(container.querySelector('[data-stack-key="standing"]')).toBeNull())
  })

  it('keeps the front slot for a held card that is not pinned', () => {
    const { container, rerender } = render(<Stack items={['reading']} />)

    rerender(<Stack items={['fresh', 'reading']} />)

    expect(container.querySelector('[data-stack-key="reading"]')?.getAttribute('data-stack-active')).toBe('true')
    expect(container.querySelector('[data-stack-key="fresh"]')).toBeNull()
  })
})
