/**
 * Turn-to-turn jumping for the transcript: which prompt row to land on when the user presses
 * Ctrl+↑ / Ctrl+↓, so reading a long session does not mean scrolling.
 *
 * The row numbers come from the virtual history's prefix offsets (`virtualHistory.offsets`):
 * `rows[i]` is item i's top row, and `rows[i + 1]` its bottom. Only real prompts count — a user
 * row with a `kind` is a slash echo or an attachment row, not something the user typed (the same
 * predicate `countPrompts` uses).
 */

export type TurnJumpDir = -1 | 1

const isPrompt = (msg: { kind?: string; role: string }): boolean => msg.role === 'user' && !msg.kind

/** Row a prompt starts on, and the 2-row gap `RewindFocusScroller` uses so the turn is not glued
 *  to the very top edge of the viewport. */
const LANDING_GAP = 2

export const turnLandingRow = (row: number): number => Math.max(0, row - LANDING_GAP)

/**
 * Index of the prompt to jump to, `-1` when there is none in that direction.
 *
 * `top` is the current viewport top in history rows. A row counts only when it is more than one
 * row away, so a viewport already resting on a prompt boundary moves PAST it instead of bouncing
 * back to where it is.
 */
export function adjacentTurnIndex(
  items: readonly { kind?: string; role: string }[],
  rows: ArrayLike<number>,
  top: number,
  dir: TurnJumpDir
): number {
  const last = Math.min(items.length, rows.length) - 1

  if (last < 0 || !Number.isFinite(top)) {
    return -1
  }

  if (dir < 0) {
    for (let i = last; i >= 0; i--) {
      if (isPrompt(items[i]!) && (rows[i] ?? 0) < top - 1) {
        return i
      }
    }

    return -1
  }

  for (let i = 0; i <= last; i++) {
    if (isPrompt(items[i]!) && (rows[i] ?? 0) > top + 1) {
      return i
    }
  }

  return -1
}

/** The slice of a scroll handle a jump needs — a real `ScrollBoxHandle` satisfies it as is. */
export interface TurnJumpHandle {
  getPendingDelta(): number
  getScrollTop(): number
  scrollTo(y: number): void
  scrollToBottom(): void
}

/**
 * Move the viewport to the adjacent turn and return the row index it landed on (`-1` when there is
 * no prompt that way). Downward past the last prompt means "show me the newest output", so it goes
 * to the bottom; upward past the first one does nothing rather than yanking the user around.
 */
export function jumpToAdjacentTurn(
  handle: TurnJumpHandle,
  items: readonly { kind?: string; role: string }[],
  rows: ArrayLike<number>,
  dir: TurnJumpDir
): number {
  const top = handle.getScrollTop() + handle.getPendingDelta()
  const index = adjacentTurnIndex(items, rows, top, dir)

  if (index < 0) {
    if (dir > 0) {
      handle.scrollToBottom()
    }

    return -1
  }

  handle.scrollTo(turnLandingRow(rows[index] ?? 0))

  return index
}
