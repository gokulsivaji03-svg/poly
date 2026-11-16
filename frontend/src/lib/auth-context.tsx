import { useQueryClient } from '@tanstack/react-query'
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router'
import { setUnauthorizedHandler } from './api'
import { clearToken, getToken, setToken } from './auth'

interface AuthContextValue {
  token: string | null
  login: (token: string, expiresAt: string | null) => void
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setTokenState] = useState<string | null>(() => getToken())
  const navigate = useNavigate()
  const qc = useQueryClient()

  const login = useCallback((t: string, expiresAt: string | null) => {
    setToken(t, expiresAt)
    setTokenState(t)
  }, [])

  const logout = useCallback(() => {
    clearToken()
    setTokenState(null)
    qc.clear()
    navigate('/login', { replace: true })
  }, [navigate, qc])

  useEffect(() => {
    setUnauthorizedHandler(logout)
    return () => setUnauthorizedHandler(null)
  }, [logout])

  const value = useMemo(() => ({ token, login, logout }), [token, login, logout])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}

/** Route guard: renders children with a guaranteed token, else redirects to /login. */
export function RequireAuth({ children }: { children: (token: string) => ReactNode }) {
  const { token } = useAuth()
  const location = useLocation()
  if (!token) return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />
  return <>{children(token)}</>
}
