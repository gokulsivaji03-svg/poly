import { useState, type KeyboardEvent } from 'react'
import { useNavigate } from 'react-router'
import { NAV } from '@/components/layout/nav'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'
import { useDashboard } from '@/lib/queries'
import { cn } from '@/lib/utils'
import { useControl } from './controls'

interface Command {
  id: string
  label: string
  hint?: string
  danger?: boolean
  run: () => void
}

function useCommands(onKill: () => void): Command[] {
  const navigate = useNavigate()
  const dash = useDashboard().data
  const pause = useControl('pause')
  const resume = useControl('resume')
  const go = NAV.map((n, i) => ({ id: `go:${n.to}`, label: `Go to ${n.label}`, hint: String(i + 1), run: () => void navigate(n.to) }))
  if (!dash || dash.emergency_stop) return go
  return [
    dash.strategies_paused
      ? { id: 'resume', label: 'Resume strategies', run: () => resume.mutate() }
      : { id: 'pause', label: 'Pause strategies', run: () => pause.mutate() },
    { id: 'kill', label: 'Emergency stop…', danger: true, run: onKill },
    ...go,
  ]
}

/** ⌘K: every page and engine control from the keyboard. */
export function CommandPalette({
  open,
  onOpenChange,
  onKill,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onKill: () => void
}) {
  const commands = useCommands(onKill)
  const [query, setQuery] = useState('')
  const [active, setActive] = useState(0)
  const shown = commands.filter((c) => c.label.toLowerCase().includes(query.trim().toLowerCase()))
  const at = Math.min(active, Math.max(shown.length - 1, 0))

  const close = () => {
    onOpenChange(false)
    setQuery('')
    setActive(0)
  }
  const run = (c: Command | undefined) => {
    if (!c) return
    close()
    c.run()
  }
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      const step = e.key === 'ArrowDown' ? 1 : -1
      setActive((at + step + shown.length) % Math.max(shown.length, 1))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      run(shown[at])
    }
  }

  return (
    <Dialog open={open} onOpenChange={(o) => (o ? onOpenChange(true) : close())}>
      <DialogContent showCloseButton={false} className="top-[18%] translate-y-0 gap-0 overflow-hidden rounded-sm border bg-popover p-0 sm:max-w-lg">
        <DialogTitle className="sr-only">Command palette</DialogTitle>
        <input
          autoFocus
          data-testid="palette-input"
          value={query}
          onChange={(e) => {
            setQuery(e.target.value)
            setActive(0)
          }}
          onKeyDown={onKey}
          placeholder="Type a command"
          aria-label="Command"
          className="h-10 w-full border-b bg-transparent px-3 text-[13px] outline-none placeholder:text-faint"
        />
        <ul role="listbox" aria-label="Commands" className="max-h-80 overflow-y-auto py-1">
          {shown.length === 0 && <li className="px-3 py-2 text-[12px] text-muted-foreground">No matching command.</li>}
          {shown.map((c, i) => (
            <li
              key={c.id}
              role="option"
              aria-selected={i === at}
              onMouseMove={() => setActive(i)}
              onClick={() => run(c)}
              className={cn(
                'flex cursor-pointer items-center justify-between px-3 py-1.5 text-[12.5px]',
                i === at && 'bg-accent',
                c.danger && 'text-neg',
              )}
            >
              {c.label}
              {c.hint && <kbd className="num rounded-[2px] border px-1 text-[10.5px] text-muted-foreground">{c.hint}</kbd>}
            </li>
          ))}
        </ul>
      </DialogContent>
    </Dialog>
  )
}
