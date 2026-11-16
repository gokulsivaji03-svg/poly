import {
  Activity,
  History,
  LayoutDashboard,
  Layers,
  ServerCog,
  Settings,
  ShieldHalf,
  Wallet,
  type LucideIcon,
} from 'lucide-react'
import { Suspense } from 'react'
import { NavLink, Outlet } from 'react-router'
import { Panel } from '@/components/common/panel'
import { SkeletonLines } from '@/components/common/states'
import { cn } from '@/lib/utils'
import { DisconnectedBanner } from './disconnected-banner'
import { TopBar } from './top-bar'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  testId: string
  end?: boolean
}

const NAV: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, testId: 'nav-dashboard', end: true },
  { to: '/wallets', label: 'Wallets', icon: Wallet, testId: 'nav-wallets' },
  { to: '/positions', label: 'Positions', icon: Layers, testId: 'nav-positions' },
  { to: '/hedges', label: 'Hedges', icon: ShieldHalf, testId: 'nav-hedges' },
  { to: '/activity', label: 'Activity', icon: Activity, testId: 'nav-activity' },
  { to: '/replay', label: 'Replay', icon: History, testId: 'nav-replay' },
  { to: '/settings', label: 'Settings', icon: Settings, testId: 'nav-settings' },
  { to: '/system', label: 'System', icon: ServerCog, testId: 'nav-system' },
]

/**
 * A single <nav> renders as a left rail on md+ and a fixed bottom bar on mobile, so every nav
 * test id exists exactly once in the DOM.
 */
function NavRail() {
  return (
    <nav
      aria-label="Primary"
      className={cn(
        'pb-safe fixed inset-x-0 bottom-0 z-40 flex border-t bg-card/95 backdrop-blur',
        'md:static md:z-auto md:w-14 md:flex-col md:border-t-0 md:border-r md:bg-card md:pt-2 md:pb-0 md:backdrop-blur-none',
        'xl:w-44',
      )}
    >
      {NAV.map(({ to, label, icon: Icon, testId, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          data-testid={testId}
          title={label}
          className={({ isActive }) =>
            cn(
              'group relative flex flex-1 flex-col items-center justify-center gap-0.5 py-1.5 text-[10px] text-muted-foreground transition-colors hover:text-foreground',
              'md:flex-none md:py-2.5 xl:flex-row xl:justify-start xl:gap-2.5 xl:px-4 xl:text-[12.5px]',
              'focus-visible:text-foreground focus-visible:outline-none',
              isActive && 'text-foreground',
            )
          }
        >
          {({ isActive }) => (
            <>
              <span
                aria-hidden
                className={cn(
                  'absolute inset-x-3 top-0 h-0.5 rounded-full bg-primary opacity-0 transition-opacity',
                  'md:inset-x-auto md:inset-y-2 md:left-0 md:h-auto md:w-0.5',
                  isActive && 'opacity-100',
                )}
              />
              <Icon className={cn('size-[18px] shrink-0 xl:size-4', isActive && 'text-primary')} strokeWidth={1.75} />
              <span className="leading-none md:sr-only xl:not-sr-only">{label}</span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  )
}

export function AppShell() {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <TopBar />
      <DisconnectedBanner />
      <div className="flex min-h-0 flex-1">
        <NavRail />
        <main className="min-w-0 flex-1 overflow-y-auto pb-20 md:pb-6">
          <div className="mx-auto flex w-full max-w-[1680px] flex-col gap-3 p-3 md:p-4">
            <Suspense
              fallback={
                <Panel>
                  <SkeletonLines rows={8} />
                </Panel>
              }
            >
              <Outlet />
            </Suspense>
          </div>
        </main>
      </div>
    </div>
  )
}
