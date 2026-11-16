import { useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api, type PatchWalletBody, type WalletDetail, type WalletSummary } from '@/lib/api'
import { qk } from '@/lib/queries'
import { errorMessage } from '@/lib/utils'

type WalletsData = { wallets: WalletSummary[] }

function replaceWallet(qc: QueryClient, wallet: WalletSummary) {
  qc.setQueryData<WalletsData>(qk.wallets, (old) =>
    old ? { wallets: old.wallets.map((w) => (String(w.id) === String(wallet.id) ? wallet : w)) } : old,
  )
  qc.setQueryData<WalletDetail>(qk.wallet(String(wallet.id)), (old) => (old ? { ...old, ...wallet } : old))
}

/** PATCH /wallets/{id}; show copy controls as changed only after the server accepts the update. */
export function usePatchWallet() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, body }: { id: string | number; body: PatchWalletBody }) => api.patchWallet(id, body),
    onError: (err) => {
      toast.error('Wallet update failed', { description: errorMessage(err) })
    },
    onSuccess: (wallet) => {
      replaceWallet(qc, wallet)
      void qc.invalidateQueries({ queryKey: qk.dashboard })
    },
  })
}

export function useDeleteWallet() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string | number) => api.deleteWallet(id),
    onSuccess: (_res, id) => {
      qc.setQueryData<WalletsData>(qk.wallets, (old) =>
        old ? { wallets: old.wallets.filter((w) => String(w.id) !== String(id)) } : old,
      )
      void qc.invalidateQueries({ queryKey: qk.dashboard })
      toast.success('Wallet removed', { description: 'Monitoring stopped; history is kept.' })
    },
  })
}
