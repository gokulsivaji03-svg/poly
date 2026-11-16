/** JWT persistence (localStorage). The token is the only credential the frontend holds. */

const TOKEN_KEY = 'polycopy.token'
const EXPIRES_KEY = 'polycopy.token_expires_at'

export function getToken(): string | null {
  try {
    const token = localStorage.getItem(TOKEN_KEY)
    if (!token) return null
    const expires = localStorage.getItem(EXPIRES_KEY)
    if (expires && Date.parse(expires) <= Date.now()) {
      clearToken()
      return null
    }
    return token
  } catch {
    return null
  }
}

export function setToken(token: string, expiresAt: string | null): void {
  localStorage.setItem(TOKEN_KEY, token)
  if (expiresAt) localStorage.setItem(EXPIRES_KEY, expiresAt)
  else localStorage.removeItem(EXPIRES_KEY)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(EXPIRES_KEY)
}
