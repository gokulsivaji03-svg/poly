import { Activity, History, LayoutDashboard, Layers, ServerCog, Settings, ShieldHalf, Wallet, type LucideIcon } from 'lucide-react'

export interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  testId: string
  end?: boolean
}

/** Order is the hotkey: the Nth item is the N key. */
export const NAV: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard, testId: 'nav-dashboard', end: true },
  { to: '/activity', label: 'Activity', icon: Activity, testId: 'nav-activity' },
  { to: '/positions', label: 'Positions', icon: Layers, testId: 'nav-positions' },
  { to: '/wallets', label: 'Wallets', icon: Wallet, testId: 'nav-wallets' },
  { to: '/replay', label: 'Replay', icon: History, testId: 'nav-replay' },
  { to: '/hedges', label: 'Hedges', icon: ShieldHalf, testId: 'nav-hedges' },
  { to: '/settings', label: 'Settings', icon: Settings, testId: 'nav-settings' },
  { to: '/system', label: 'System', icon: ServerCog, testId: 'nav-system' },
]
