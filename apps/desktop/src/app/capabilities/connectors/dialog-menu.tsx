import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  dropdownMenuRow,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { openConnectorsAdmin } from './data/portal'

export interface ConnectorDialogMenuProps {
  onDisconnect?: () => void
  onReconnect?: () => void
  onRefreshTools: () => void
  /** Respawn a local server in place — the rebuilt-binary case a config reload cannot fix. */
  onRestart?: () => void
  restartBusy?: boolean
}

export function ConnectorDialogMenu({
  onDisconnect,
  onReconnect,
  onRefreshTools,
  onRestart,
  restartBusy = false
}: ConnectorDialogMenuProps) {
  const { t } = useI18n()
  const copy = t.connectorsPage

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button aria-label={copy.dialog.moreActions} size="icon-xs" variant="ghost">
          <Codicon name="ellipsis" size="0.8125rem" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        {onReconnect ? (
          <DropdownMenuItem className={dropdownMenuRow} onSelect={onReconnect}>
            {copy.card.verb.reconnect}
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuItem className={dropdownMenuRow} onSelect={onRefreshTools}>
          {copy.dialog.menuRefreshTools}
        </DropdownMenuItem>
        {onRestart ? (
          <DropdownMenuItem className={dropdownMenuRow} disabled={restartBusy} onSelect={onRestart}>
            {copy.dialog.menuRestartServer}
          </DropdownMenuItem>
        ) : null}
        <DropdownMenuSeparator />
        <DropdownMenuItem className={dropdownMenuRow} onSelect={() => void openConnectorsAdmin()}>
          {copy.dialog.orgLink}
        </DropdownMenuItem>
        {onDisconnect ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              className={cn(dropdownMenuRow, 'text-destructive focus:text-destructive')}
              onSelect={onDisconnect}
            >
              {copy.dialog.disconnect}
            </DropdownMenuItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
