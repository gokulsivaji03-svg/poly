import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { lazy } from 'react'
import { BrowserRouter, Route, Routes } from 'react-router'
import { AppShell } from '@/components/layout/app-shell'
import { Toaster } from '@/components/ui/sonner'
import { TooltipProvider } from '@/components/ui/tooltip'
import { ApiError } from '@/lib/api'
import { AuthProvider, RequireAuth } from '@/lib/auth-context'
import { RealtimeProvider } from '@/lib/realtime'
import { LoginPage } from '@/pages/login'
import { NotFoundPage } from '@/pages/not-found'

// Route-level code splitting keeps the initial bundle small (charts load with the pages using them).
const DashboardPage = lazy(() => import('@/pages/dashboard').then((m) => ({ default: m.DashboardPage })))
const WalletsPage = lazy(() => import('@/pages/wallets').then((m) => ({ default: m.WalletsPage })))
const WalletDetailPage = lazy(() => import('@/pages/wallet-detail').then((m) => ({ default: m.WalletDetailPage })))
const PositionsPage = lazy(() => import('@/pages/positions').then((m) => ({ default: m.PositionsPage })))
const HedgesPage = lazy(() => import('@/pages/hedges').then((m) => ({ default: m.HedgesPage })))
const ActivityPage = lazy(() => import('@/pages/activity').then((m) => ({ default: m.ActivityPage })))
const ReplayPage = lazy(() => import('@/pages/replay').then((m) => ({ default: m.ReplayPage })))
const SettingsPage = lazy(() => import('@/pages/settings').then((m) => ({ default: m.SettingsPage })))
const SystemPage = lazy(() => import('@/pages/system').then((m) => ({ default: m.SystemPage })))

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      refetchOnWindowFocus: false,
      // Client errors (auth, validation, not found) won't fix themselves; retry transient ones.
      retry: (count, error) => !(error instanceof ApiError && error.status >= 400 && error.status < 500) && count < 2,
    },
  },
})

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={250}>
        <BrowserRouter>
          <AuthProvider>
            <Routes>
              <Route path="/login" element={<LoginPage />} />
              <Route
                element={
                  <RequireAuth>
                    {(token) => (
                      <RealtimeProvider token={token}>
                        <AppShell />
                      </RealtimeProvider>
                    )}
                  </RequireAuth>
                }
              >
                <Route index element={<DashboardPage />} />
                <Route path="wallets" element={<WalletsPage />} />
                <Route path="wallets/:id" element={<WalletDetailPage />} />
                <Route path="positions" element={<PositionsPage />} />
                <Route path="hedges" element={<HedgesPage />} />
                <Route path="activity" element={<ActivityPage />} />
                <Route path="replay" element={<ReplayPage />} />
                <Route path="settings" element={<SettingsPage />} />
                <Route path="system" element={<SystemPage />} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>
            </Routes>
          </AuthProvider>
        </BrowserRouter>
        <Toaster position="bottom-right" mobileOffset={{ bottom: 72 }} toastOptions={{ className: 'text-[12.5px]' }} />
      </TooltipProvider>
    </QueryClientProvider>
  )
}
