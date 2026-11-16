import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useCallback, useMemo, useState } from 'react'
import { toast } from 'sonner'
import { api, ApiError, type SettingsPatch } from '@/lib/api'
import { useSettings } from '@/lib/queries'
import { applySettingsEnvelope } from '@/lib/realtime-cache'
import { errorMessage } from '@/lib/utils'
import { buildPatch, displayFromSettings, pathOf, sameValue, validate, type FieldDef } from './schema'

export interface SettingsForm {
  ready: boolean
  version: number | null
  value: (f: FieldDef) => string
  setValue: (f: FieldDef, display: string) => void
  isDirty: (f: FieldDef) => boolean
  error: (f: FieldDef) => string | null
  dirtyCount: number
  hasErrors: boolean
  saving: boolean
  /** Server reported a version conflict (409); `reload` rebases pending edits on the latest. */
  conflict: boolean
  formError: string | null
  save: () => void
  discard: () => void
  reload: () => Promise<void>
  /** A change made by this client (e.g. a control action) moved the server from v-1 to v: keep edits current. */
  rebaseAfterOwnChange: (newVersion: number) => void
}

/**
 * Edit-tracking form over GET /settings. Edits are kept as display strings keyed by path and
 * rebased automatically when the server copy changes; Save sends only changed fields with the
 * version the edits were started against.
 */
export function useSettingsForm(fields: readonly FieldDef[], { successMessage = 'Settings saved' } = {}): SettingsForm {
  const qc = useQueryClient()
  const q = useSettings()
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [baseVersion, setBaseVersion] = useState<number | null>(null)
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({})
  const [conflict, setConflict] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)

  const settings = q.data?.settings
  const base = useCallback((f: FieldDef) => (settings ? displayFromSettings(settings, f) : ''), [settings])
  const value = useCallback((f: FieldDef) => edits[pathOf(f)] ?? base(f), [edits, base])

  const dirtyFields = useMemo(
    () => fields.filter((f) => edits[pathOf(f)] !== undefined && !sameValue(f, edits[pathOf(f)]!, base(f))),
    [fields, edits, base],
  )
  const clientErrors = useMemo(() => {
    const out: Record<string, string> = {}
    for (const f of dirtyFields) {
      const msg = validate(f, edits[pathOf(f)]!)
      if (msg) out[pathOf(f)] = msg
    }
    return out
  }, [dirtyFields, edits])

  const mutation = useMutation({
    mutationFn: ({ patch, expected }: { patch: SettingsPatch; expected: number | undefined }) => api.putSettings(patch, expected),
    onSuccess: (env) => {
      applySettingsEnvelope(qc, env)
      setEdits({})
      setBaseVersion(null)
      setServerErrors({})
      setConflict(false)
      setFormError(null)
      toast.success(successMessage, { description: `Version ${env.version}` })
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 409) {
        setConflict(true)
        return
      }
      if (err instanceof ApiError && err.status === 422) {
        const fe = err.fieldErrors
        const known = new Set(fields.map(pathOf))
        const mapped = Object.fromEntries(Object.entries(fe).filter(([p]) => known.has(p)))
        setServerErrors(mapped)
        setFormError(Object.keys(mapped).length === 0 ? err.message : 'Some values were rejected — see highlighted fields.')
        return
      }
      setFormError(errorMessage(err))
    },
  })

  const setValue = useCallback(
    (f: FieldDef, display: string) => {
      const path = pathOf(f)
      if (Object.keys(edits).length === 0 && q.data) setBaseVersion(q.data.version)
      setEdits((prev) => ({ ...prev, [path]: display }))
      setServerErrors((prev) => {
        if (!(path in prev)) return prev
        const next = { ...prev }
        delete next[path]
        return next
      })
    },
    [edits, q.data],
  )

  const rebaseAfterOwnChange = useCallback((newVersion: number) => {
    setBaseVersion((v) => (v !== null && v === newVersion - 1 ? newVersion : v))
  }, [])

  const save = () => {
    if (dirtyFields.length === 0 || Object.keys(clientErrors).length > 0) return
    setFormError(null)
    const patch = buildPatch(dirtyFields.map((f) => ({ field: f, display: edits[pathOf(f)]! })))
    mutation.mutate({ patch, expected: baseVersion ?? q.data?.version })
  }

  const discard = () => {
    setEdits({})
    setBaseVersion(null)
    setServerErrors({})
    setConflict(false)
    setFormError(null)
  }

  const reload = async () => {
    const res = await q.refetch()
    if (res.data) setBaseVersion(res.data.version)
    setConflict(false)
    setFormError(null)
  }

  return {
    ready: Boolean(settings),
    version: q.data?.version ?? null,
    value,
    setValue,
    isDirty: (f) => dirtyFields.includes(f),
    error: (f) => clientErrors[pathOf(f)] ?? serverErrors[pathOf(f)] ?? null,
    dirtyCount: dirtyFields.length,
    hasErrors: Object.keys(clientErrors).length > 0,
    saving: mutation.isPending,
    conflict,
    formError,
    save,
    discard,
    reload,
    rebaseAfterOwnChange,
  }
}
