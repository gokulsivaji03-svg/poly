import { LogOut, OctagonX, PauseCircle, User } from 'lucide-react'
import { Link } from 'react-router'
import { Tag } from '@/components/common/badges'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useAuth } from '@/lib/auth-context'
import { useDashboard, useMe } from '@/lib/queries'
import { useRealtime } from '@/lib/realtime-context'
import { cn } from '@/lib/utils'
import type { ConnState } from '@/lib/ws'

const CONN_LABEL: Record<ConnState, string> = {
  connected: 'Live',
  connecting: 'Connecting',
  disconnected: 'Offline',
}

const CONN_DOT: Record<ConnState, string> = {
  connected: 'bg-pos',
  connecting: 'bg-warn animate-pulse',
  disconnected: 'bg-neg',
}

function ConnStatus() {
  const { status } = useRealtime()
  const detail =
    status.state === 'connected'
      ? 'Realtime stream connected'
      : status.state === 'connecting'
        ? `Connecting to realtime stream${status.attempt > 0 ? ` (attempt ${status.attempt + 1})` : ''}`
        : 'Realtime stream disconnected — polling every 10s while reconnecting'
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          data-testid="conn-status"
          data-state={status.state}
          role="status"
          aria-label={`Connection: ${CONN_LABEL[status.state]}`}
          tabIndex={0}
          className="flex h-6 items-center gap-1.5 rounded-sm border px-2 text-[11px] text-muted-foreground outline-none"
        >
          <span className={cn('size-1.5 rounded-full', CONN_DOT[status.state])} aria-hidden />
          <span className="hidden sm:inline">{CONN_LABEL[status.state]}</span>
        </div>
      </TooltipTrigger>
      <TooltipContent>{detail}</TooltipContent>
    </Tooltip>
  )
}

function UserMenu() {
  const { logout } = useAuth()
  const me = useMe()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" aria-label="Account" className="text-muted-foreground">
          <User />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        <DropdownMenuLabel className="text-[12px] font-normal text-muted-foreground">
          Signed in as <span className="text-foreground">{me.data?.username ?? '…'}</span>
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={logout} className="text-[12.5px]">
          <LogOut /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function TopBar() {
  const { data } = useDashboard()
  const mode = data?.mode
  return (
    <header className="flex h-11 shrink-0 items-center gap-2 border-b bg-card px-3 md:gap-3">
      <Link to="/" className="flex items-center gap-2 pr-1 focus-visible:outline-none" aria-label="polycopy home">
        <span className="grid size-6 place-items-center rounded-sm bg-primary text-[11px] font-bold text-primary-foreground">
          pc
        </span>
        <span className="hidden text-[13px] font-semibold tracking-tight sm:inline">polycopy</span>
      </Link>
      <div className="h-5 w-px bg-border" aria-hidden />
      {mode ? (
        <Tag
          tone={mode === 'LIVE' ? 'danger' : mode === 'REPLAY' ? 'replay' : 'accent'}
          title={mode === 'PAPER' ? 'Paper trading — simulated fills against live books' : `${mode} mode`}
        >
          <span data-testid="mode-badge">{mode}</span>
        </Tag>
      ) : (
        <span className="h-[18px] w-12 animate-pulse rounded-sm bg-muted" aria-hidden />
      )}
      {data?.emergency_stop && (
        <Link to="/settings#controls" className="focus-visible:outline-none">
          <Tag tone="danger" className="gap-1">
            <OctagonX className="size-3" /> Emergency stop
          </Tag>
        </Link>
      )}
      {data?.strategies_paused && !data.emergency_stop && (
        <Link to="/settings#controls" className="focus-visible:outline-none">
          <Tag tone="warn" className="gap-1">
            <PauseCircle className="size-3" /> Paused
          </Tag>
        </Link>
      )}
      {data && (
        <div className="hidden items-center gap-2 text-[11px] text-muted-foreground lg:flex">
          <span>
            copy <span className={data.copy_enabled ? 'text-foreground' : 'text-faint'}>{data.copy_enabled ? 'on' : 'off'}</span>
          </span>
          <span className="text-faint">/</span>
          <span>
            hedge{' '}
            <span className={data.hedge_enabled ? 'text-foreground' : 'text-faint'}>
              {data.hedge_enabled ? (data.hedge_mode === 'AUTO_PAPER' ? 'auto' : 'alert') : 'off'}
            </span>
          </span>
        </div>
      )}
      <div className="ml-auto flex items-center gap-1.5">
        <ConnStatus />
        <UserMenu />
      </div>
    </header>
  )
}
