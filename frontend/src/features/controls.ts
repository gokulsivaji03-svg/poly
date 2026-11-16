import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api, type SettingsEnvelope } from '@/lib/api'
import { applySettingsEnvelope } from '@/lib/realtime-cache'
import { errorMessage } from '@/lib/utils'

export type ControlAction = 'pause' | 'resume' | 'emergencyStop' | 'clearEmergency'

const MESSAGES: Record<ControlAction, string> = {
  pause: 'All strategies paused',
  resume: 'Strategies resumed',
  emergencyStop: 'Emergency stop engaged — reservations released',
  clearEmergency: 'Emergency stop cleared',
}

/** Strategy control actions (POST /control/*); results are written straight into the cache. */
export function useControl(action: ControlAction, onApplied?: (env: SettingsEnvelope) => void) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => api[action](),
    onSuccess: (env) => {
      applySettingsEnvelope(qc, env)
      onApplied?.(env)
      toast.success(MESSAGES[action])
    },
    onError: (err) => toast.error('Control action failed', { description: errorMessage(err) }),
  })
}
