import { Suspense, useCallback, useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router'
import { Panel } from '@/components/common/panel'
import { SkeletonLines } from '@/components/common/states'
import { CommandPalette } from '@/features/command-palette'
import { KillDialog } from '@/features/kill-dialog'
import { cn } from '@/lib/utils'
import { DisconnectedBanner } from './disconnected-banner'
import { NAV } from './nav'
import { TopBar } from './top-bar'

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
        'md:static md:z-auto md:w-12 md:flex-col md:border-t-0 md:border-r md:bg-card md:pt-1 md:pb-0 md:backdrop-blur-none',
      )}
    >
      {NAV.map(({ to, label, icon: Icon, testId, end }, i) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          data-testid={testId}
          title={`${label}  ${i + 1}`}
          className={({ isActive }) =>
            cn(
              'group relative flex flex-1 flex-col items-center justify-center gap-0.5 py-1.5 text-[10px] text-muted-foreground transition-colors hover:text-foreground',
              'md:flex-none md:py-2.5',
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
              <Icon className={cn('size-[18px] shrink-0 md:size-4', isActive && 'text-primary')} strokeWidth={1.75} />
              <span className="leading-none md:sr-only">{label}</span>
            </>
          )}
        </NavLink>
      ))}
    </nav>
  )
}

/** Number keys switch pages; ⌘K or Ctrl+K opens the palette. Typing in a field or dialog is never hijacked. */
function useHotkeys(togglePalette: () => void) {
  const navigate = useNavigate()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        togglePalette()
        return
      }
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return
      const target = e.target as HTMLElement | null
      if (target?.closest('input, textarea, select, [contenteditable="true"], [role="dialog"], [role="alertdialog"]')) return
      const item = NAV[Number(e.key) - 1]
      if (item) void navigate(item.to)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate, togglePalette])
}

export function AppShell() {
  const [palette, setPalette] = useState(false)
  const [kill, setKill] = useState(false)
  useHotkeys(useCallback(() => setPalette((o) => !o), []))
  return (
    <div className="flex h-full min-h-0 flex-col">
      <TopBar onPalette={() => setPalette(true)} onKill={() => setKill(true)} />
      <CommandPalette open={palette} onOpenChange={setPalette} onKill={() => setKill(true)} />
      <KillDialog open={kill} onOpenChange={setKill} />
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
