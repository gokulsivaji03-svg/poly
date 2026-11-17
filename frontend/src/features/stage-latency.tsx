import { Num } from '@/components/common/num'
import { Panel } from '@/components/common/panel'
import { EmptyState } from '@/components/common/states'
import type { CopyStage } from '@/lib/api'

export function StageLatency({ stages }: { stages: CopyStage[] }) {
  const steps = stages.filter((s) => s.stage !== 'total')
  const total = stages.find((s) => s.stage === 'total')
  const widest = Math.max(...steps.map((s) => s.p50_ms), 0)
  return (
    <Panel title="Copy latency by stage" actions={total ? <span className="num text-[11px] text-muted-foreground">last {total.n} copies</span> : undefined}>
      {steps.length === 0 ? (
        <EmptyState title="No timed copies yet" description="Each copy records monotonic time from fill receipt to every pipeline stage." />
      ) : (
        <table className="w-full text-[12px]" aria-label="Copy latency by stage">
          <thead className="text-muted-foreground">
            <tr className="[&>th]:px-3 [&>th]:py-1.5 [&>th]:text-left [&>th]:font-normal">
              <th>Stage</th>
              <th className="w-1/2" aria-label="p50 share" />
              <th className="text-right">p50</th>
              <th className="text-right">p99</th>
            </tr>
          </thead>
          <tbody>
            {steps.map((s) => (
              <tr key={s.stage} className="border-t [&>td]:px-3 [&>td]:py-1.5">
                <td className="num">{s.stage}</td>
                <td>
                  <div className="h-1.5 rounded-full bg-muted">
                    <div className="h-1.5 rounded-full bg-primary" style={{ width: `${widest ? (s.p50_ms / widest) * 100 : 0}%` }} />
                  </div>
                </td>
                <td className="text-right"><Num value={s.p50_ms} kind="ms" /></td>
                <td className="text-right"><Num value={s.p99_ms} kind="ms" /></td>
              </tr>
            ))}
            {total ? (
              <tr className="border-t font-medium [&>td]:px-3 [&>td]:py-1.5">
                <td className="num" title="Fill receipt to order booked">total</td>
                <td />
                <td className="text-right"><Num value={total.p50_ms} kind="ms" /></td>
                <td className="text-right"><Num value={total.p99_ms} kind="ms" /></td>
              </tr>
            ) : null}
          </tbody>
        </table>
      )}
    </Panel>
  )
}
