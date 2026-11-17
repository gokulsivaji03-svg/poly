import { Loader2 } from 'lucide-react'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { useControl } from './controls'

/** Emergency stop behind one confirmation. Opened from the top bar or the command palette. */
export function KillDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const kill = useControl('emergencyStop', () => onOpenChange(false))
  return (
    <AlertDialog open={open} onOpenChange={(o) => !kill.isPending && onOpenChange(o)}>
      <AlertDialogContent className="rounded-sm border bg-card sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="text-[15px]">Engage emergency stop?</AlertDialogTitle>
          <AlertDialogDescription className="text-[12.5px] leading-relaxed">
            Every strategy stops and open reservations are released. Positions stay open. Clearing it takes a deliberate
            action on the Settings page.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel size="sm" disabled={kill.isPending}>
            Cancel
          </AlertDialogCancel>
          <Button size="sm" variant="destructive" data-testid="kill-confirm" disabled={kill.isPending} onClick={() => kill.mutate()}>
            {kill.isPending && <Loader2 className="animate-spin" />}
            Stop everything
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
