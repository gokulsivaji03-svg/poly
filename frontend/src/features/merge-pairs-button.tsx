import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Combine } from 'lucide-react'
import { toast } from 'sonner'
import { ConfirmDialog } from '@/components/common/confirm-dialog'
import { Num } from '@/components/common/num'
import { KV } from '@/components/common/panel'
import { Button } from '@/components/ui/button'
import { api, type HedgeState } from '@/lib/api'
import { fmtUsd } from '@/lib/format'
import { qk } from '@/lib/queries'

const MERGEABLE: Partial<Record<HedgeState, true>> = { FULLY_HEDGED: true, PARTIALLY_HEDGED: true }

export function isMergeable(state: string): boolean {
  return Boolean(MERGEABLE[state as HedgeState])
}

/**
 * Simulated CTF merge of matched YES/NO pairs (POST /hedges/{id}/merge, paper only).
 * `hedgeId` is the opportunity id "<condition_id>:<primary_token_id>"; the API client URL-encodes it.
 */
export function MergePairsButton({
  hedgeId,
  question,
  matchedShares,
}: {
  hedgeId: string
  question: string
  matchedShares: string | null
}) {
  const qc = useQueryClient()
  const mutation = useMutation({
    mutationFn: () => api.mergeHedge(hedgeId),
    onSuccess: (res) => {
      toast.success('Simulated merge complete', {
        description: res.proceeds != null ? `${fmtUsd(res.proceeds) ?? res.proceeds} released to paper cash` : undefined,
      })
      void qc.invalidateQueries({ queryKey: qk.positions })
      void qc.invalidateQueries({ queryKey: qk.hedges })
      void qc.invalidateQueries({ queryKey: qk.hedgeEvents })
      void qc.invalidateQueries({ queryKey: qk.dashboard })
      void qc.invalidateQueries({ queryKey: ['equity-history'] })
    },
  })
  return (
    <ConfirmDialog
      trigger={
        <Button size="xs" variant="outline" data-testid="hedge-merge">
          <Combine /> Merge pairs (simulated)
        </Button>
      }
      title="Merge matched pairs (simulated)?"
      description={
        <>
          Simulates merging the matched YES/NO pairs in “{question}” back into USDC at $1.00 per pair, in the paper ledger
          only. This is a simulation: no on-chain transaction is sent, and real CTF merges are not instantaneous — they
          require a Polygon transaction and confirmation time.
        </>
      }
      confirmLabel="Simulate merge"
      onConfirm={() => mutation.mutateAsync()}
    >
      <div className="rounded-sm border bg-background/60 px-3 py-1.5">
        <KV label="Matched pairs">
          <Num value={matchedShares} kind="shares" />
        </KV>
      </div>
    </ConfirmDialog>
  )
}
