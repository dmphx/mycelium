import type { ReactNode } from 'react';

export type IconName = 'discover' | 'library' | 'watchlist' | 'search' | 'requests' | 'settings' | 'wanted' | 'admin' | 'manual' | 'menu' | 'close' | 'chevron-right' | 'chevron-down' | 'logout' | 'user' | 'check' | 'plus' | 'play' | 'arrow-right' | 'arrow-left' | 'warning';

const shapes: Record<IconName, ReactNode> = {
  discover: <><circle cx="12" cy="12" r="9"/><path d="m16 8-2.5 5.5L8 16l2.5-5.5L16 8Z"/></>,
  library: <><path d="M4 4v16M9 4v16M14 5l5-1 3 15-5 1-3-15Z"/></>,
  watchlist: <path d="M6 4h12v17l-6-4-6 4V4Z"/>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  requests: <><path d="M4 5h16M4 10h8M4 19h8m4-9 5 5-5 5M9 15h12"/></>,
  settings: <><path d="M5 3v8m0 4v6M12 3v3m0 4v11M19 3v12m0 4v2"/><path d="M2 11h6m1-5h6m1 9h6"/></>,
  wanted: <><circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/></>,
  admin: <><path d="m12 3 8 3v6c0 5-8 9-8 9S4 17 4 12V6l8-3Z"/><path d="m8 12 3 3 5-6"/></>,
  manual: <><path d="M12 5v15M3 4h5c2 0 4 1 4 3 0-2 2-3 4-3h5v15h-5c-2 0-4 1-4 2-1-1-2-2-4-2H3V4Z"/></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16"/>,
  close: <path d="m6 6 12 12M18 6 6 18"/>,
  'chevron-right': <path d="m9 5 7 7-7 7"/>,
  'chevron-down': <path d="m5 9 7 7 7-7"/>,
  logout: <><path d="M10 4H4v16h6m5-14 6 6-6 6M9 12h12"/></>,
  user: <><circle cx="12" cy="8" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/></>,
  check: <path d="m4 12 5 5L20 6"/>,
  plus: <path d="M12 4v16M4 12h16"/>,
  play: <path d="m8 4 13 8-13 8V4Z"/>,
  'arrow-right': <path d="M4 12h16m-6-6 6 6-6 6"/>,
  'arrow-left': <path d="M20 12H4m6-6-6 6 6 6"/>,
  warning: <><path d="m12 3 10 18H2L12 3Z"/><path d="M12 9v5m0 3h.01"/></>,
};

export default function Icon({ name, size = 18, className = '' }: { name: IconName; size?: number; className?: string }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" className={`shrink-0 ${className}`}>{shapes[name]}</svg>;
}
