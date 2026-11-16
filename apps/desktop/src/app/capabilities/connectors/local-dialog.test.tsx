import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import type { McpServersController } from '../mcp/use-mcp-servers'

import { LocalConnectorDialog } from './local-dialog'
import type { ConnectorCardModel } from './types'

// Radix menus use pointer capture and scrollIntoView; jsdom has neither.
beforeAll(() => {
  Element.prototype.hasPointerCapture ??= () => false
  Element.prototype.releasePointerCapture ??= () => undefined
  Element.prototype.scrollIntoView ??= () => undefined
})

afterEach(() => {
  cleanup()
})

const card = (over: Partial<ConnectorCardModel> = {}): ConnectorCardModel => ({
  inCatalog: true,
  name: 'Swap',
  residency: 'local',
  slug: 'swap',
  state: 'connected',
  stateWord: 'serverOn',
  ways: { hosted: null, local: { installed: true, serverEnabled: true, serverName: 'swap', state: 'connected' } },
  ...over
})

// The dialog reads the whole controller; only the restart surface matters here.
const controller = (over: Partial<McpServersController> = {}) =>
  ({
    activeBlock: null,
    blocks: [],
    costFor: () => ({ tokens: null, uses: null }),
    dirty: false,
    draft: '{}',
    probes: {},
    restartServer: vi.fn(async () => undefined),
    restarting: null,
    runProbe: vi.fn(async () => undefined),
    saving: false,
    servers: { swap: { command: 'swap-mcp' } },
    ...over
  }) as unknown as McpServersController

const renderDialog = (target: ConnectorCardModel, ctl: McpServersController) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
      <LocalConnectorDialog
        card={target}
        controller={ctl}
        onClose={vi.fn()}
        onConnect={vi.fn()}
        onInstall={vi.fn()}
        onReconnect={vi.fn()}
        onRemoveServer={vi.fn()}
        profile="default"
      />
      </MemoryRouter>
    </QueryClientProvider>
  )

const restartWith = (ctl: McpServersController) => {
  fireEvent.pointerDown(screen.getByRole('button', { name: 'More actions' }), {
    button: 0,
    ctrlKey: false,
    pointerType: 'mouse'
  })
  fireEvent.click(screen.getByText('Restart server'))

  return ctl
}

describe('the local connector dialog', () => {
  it('respawns this server from its own actions menu', () => {
    const ctl = controller()

    renderDialog(card(), ctl)
    restartWith(ctl)

    expect(ctl.restartServer).toHaveBeenCalledWith('swap')
  })

  it('offers no restart for a server a plugin provides — the plugin owns the process', () => {
    const ctl = controller({ servers: { swap: { command: 'swap-mcp' } } })

    renderDialog(card({ plugin: 'swap-plugin' }), ctl)
    fireEvent.pointerDown(screen.getByRole('button', { name: 'More actions' }), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    })

    expect(screen.queryByText('Restart server')).toBeNull()
  })
})
