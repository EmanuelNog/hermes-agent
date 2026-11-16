import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { ConnectorDialogMenu } from './dialog-menu'

// Radix menus use pointer capture and scrollIntoView; jsdom has neither.
beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.scrollIntoView ??= () => undefined
})

afterEach(() => {
  cleanup()
})

const openMenu = () => {
  const trigger = screen.getByRole('button', { name: 'More actions' })

  // Radix opens on pointerdown, not click: jsdom's click alone leaves the menu closed.
  fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerType: 'mouse' })
  fireEvent.click(trigger)
}

describe('the local server actions menu', () => {
  it('offers a restart that respawns the server', () => {
    const onRestart = vi.fn()

    render(<ConnectorDialogMenu onRefreshTools={vi.fn()} onRestart={onRestart} />)
    openMenu()
    fireEvent.click(screen.getByText('Restart server'))

    expect(onRestart).toHaveBeenCalledTimes(1)
  })

  it('offers no restart where the caller cannot restart (a hosted connector)', () => {
    render(<ConnectorDialogMenu onRefreshTools={vi.fn()} />)
    openMenu()

    expect(screen.queryByText('Restart server')).toBeNull()
  })
})
