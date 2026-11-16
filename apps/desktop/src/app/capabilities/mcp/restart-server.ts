import type { RpcMethods } from '@hermes/shared'

/** `mcp.servers.restart` answers the row's runtime status and how many tools it serves. */
export type McpRestartAnswer = RpcMethods['mcp.servers.restart']['result']

export interface RestartReport {
  error: string | undefined
  name: string
  ok: boolean
  status: McpRestartAnswer['status']
  tools: number
}

export interface RestartServerSteps {
  /** Re-list the server's tools: the RPC answers a count, not names. */
  refreshTools: (name: string) => void
  report: (outcome: RestartReport) => void
}

const messageOf = (error: unknown): string => (error instanceof Error ? error.message : String(error))

/**
 * Respawn one server in the running gateway and turn its answer into a report plus a tool refresh.
 * The status comes from the owner that did the reconnect, so it is the row's truth until the next probe.
 */
export async function restartServer(
  name: string,
  call: (name: string) => Promise<McpRestartAnswer>,
  steps: RestartServerSteps
): Promise<void> {
  let answer: McpRestartAnswer

  try {
    answer = await call(name)
  } catch (error) {
    // An older backend has no such method at all; that reads as a failed restart, not a crash.
    steps.report({ error: messageOf(error), name, ok: false, status: 'failed', tools: 0 })

    return
  }

  steps.report({ error: answer.error ?? undefined, name, ok: answer.ok, status: answer.status, tools: answer.tools })

  if (answer.ok) {
    steps.refreshTools(name)
  }
}
