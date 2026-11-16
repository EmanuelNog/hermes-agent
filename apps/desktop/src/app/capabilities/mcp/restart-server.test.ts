import { describe, expect, it, vi } from 'vitest'

import { type McpRestartAnswer, restartServer } from './restart-server'

const connected: McpRestartAnswer = {
  name: 'swap',
  ok: true,
  restarted: true,
  status: 'connected',
  tools: 6
}

describe('restarting one MCP server', () => {
  it('reports the status and tool count, then re-lists the tools', async () => {
    const call = vi.fn(async () => connected)
    const report = vi.fn()
    const refreshTools = vi.fn()

    await restartServer('swap', call, { refreshTools, report })

    expect(call).toHaveBeenCalledWith('swap')
    expect(report).toHaveBeenCalledWith({ error: undefined, name: 'swap', ok: true, status: 'connected', tools: 6 })
    expect(refreshTools).toHaveBeenCalledWith('swap')
  })

  it('keeps a refused respawn on the row and skips the tool refresh', async () => {
    const call = vi.fn(async () => ({
      ...connected,
      ok: false,
      restarted: false,
      status: 'failed' as const,
      tools: 0,
      error: 'spawn ENOENT'
    }))

    const report = vi.fn()
    const refreshTools = vi.fn()

    await restartServer('swap', call, { refreshTools, report })

    expect(report).toHaveBeenCalledWith({ error: 'spawn ENOENT', name: 'swap', ok: false, status: 'failed', tools: 0 })
    expect(refreshTools).not.toHaveBeenCalled()
  })

  it('reports a transport rejection (an older backend without the verb) as a failure', async () => {
    const call = vi.fn(async (): Promise<McpRestartAnswer> => {
      throw new Error('unknown_method')
    })

    const report = vi.fn()
    const refreshTools = vi.fn()

    await restartServer('swap', call, { refreshTools, report })

    expect(report).toHaveBeenCalledWith({ error: 'unknown_method', name: 'swap', ok: false, status: 'failed', tools: 0 })
    expect(refreshTools).not.toHaveBeenCalled()
  })
})
