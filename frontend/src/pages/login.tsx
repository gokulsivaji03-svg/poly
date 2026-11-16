import { Loader2 } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/lib/auth-context'
import { errorMessage } from '@/lib/utils'

export function LoginPage() {
  const { token, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = (location.state as { from?: string } | null)?.from ?? '/'
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)

  if (token) return <Navigate to={from} replace />

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setPending(true)
    setError(null)
    try {
      const res = await api.login(username, password)
      login(res.access_token, res.expires_at ?? null)
      navigate(from.startsWith('/login') ? '/' : from, { replace: true })
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? 'Invalid username or password' : errorMessage(err))
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="flex min-h-full items-center justify-center bg-background p-4">
      <div className="w-full max-w-[360px]">
        <div className="mb-6 flex items-center gap-2.5">
          <span className="grid size-8 place-items-center rounded-sm bg-primary text-[13px] font-bold text-primary-foreground">
            pc
          </span>
          <div>
            <div className="text-[15px] font-semibold tracking-tight">polycopy</div>
            <div className="label-caps">Copy-trading &amp; hedging terminal</div>
          </div>
        </div>
        <form onSubmit={submit} className="flex flex-col gap-4 rounded-sm border bg-card p-5" noValidate>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="login-username" className="label-caps">
              Username
            </Label>
            <Input
              id="login-username"
              data-testid="login-username"
              autoComplete="username"
              autoFocus
              required
              value={username}
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="login-password" className="label-caps">
              Password
            </Label>
            <Input
              id="login-password"
              data-testid="login-password"
              type="password"
              autoComplete="current-password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          {error && (
            <p role="alert" className="rounded-sm border border-neg/30 bg-neg/10 px-2.5 py-1.5 text-[12px] text-neg">
              {error}
            </p>
          )}
          <Button type="submit" data-testid="login-submit" disabled={pending || !username || !password}>
            {pending && <Loader2 className="animate-spin" />}
            Sign in
          </Button>
        </form>
      </div>
    </div>
  )
}
