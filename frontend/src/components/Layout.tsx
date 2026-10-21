import { useEffect, useRef, useState } from 'react';
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import Brand from './Brand';
import Icon, { type IconName } from './Icon';
import { appName } from '../branding';
import { api } from '../api';
import type { SessionInfo } from '../types';

type NavigationItem = { to: string; label: string; icon: IconName; exact?: boolean };
const navItems: NavigationItem[] = [
  { to: '/', label: 'Discover', icon: 'discover', exact: true },
  { to: '/library', label: 'Library', icon: 'library' },
  { to: '/watchlist', label: 'Watchlist', icon: 'watchlist' },
  { to: '/search', label: 'Search', icon: 'search' },
  { to: '/requests', label: 'My requests', icon: 'requests' },
  { to: '/settings', label: 'Settings', icon: 'settings' },
];
const adminItems: NavigationItem[] = [
  { to: '/wanted', label: 'Wanted', icon: 'wanted' },
  { to: '/admin', label: 'Admin', icon: 'admin' },
  { to: '/manual', label: 'Manual', icon: 'manual' },
];
const routeTitle = (path: string) => [...navItems, ...adminItems].find(item => item.to === path)?.label || appName;

export default function Layout() {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawer = useRef<HTMLDialogElement>(null);
  const location = useLocation();
  const session = useQuery({ queryKey: ['session'], queryFn: api.session, staleTime: 60_000 });
  const showAdmin = session.data?.user?.role === 'admin' || session.data?.authenticated === false;
  const close = () => setDrawerOpen(false);

  useEffect(() => {
    const dialog = drawer.current;
    if (!dialog) return;
    if (!drawerOpen) { if (dialog.open) dialog.close(); return; }
    const oldOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    dialog.showModal();
    return () => { if (dialog.open) dialog.close(); document.body.style.overflow = oldOverflow; };
  }, [drawerOpen]);
  useEffect(close, [location.pathname]);
  useEffect(() => {
    const wide = window.matchMedia('(min-width: 1024px)');
    const resize = () => { if (wide.matches) close(); };
    wide.addEventListener('change', resize);
    return () => wide.removeEventListener('change', resize);
  }, []);

  const sidebar = (mobile: boolean) => <div className="flex min-h-0 h-full flex-col">
    <div className="flex min-h-[92px] items-center justify-between gap-3 px-5">
      <NavLink to="/" onClick={close} aria-label={`${appName} home`} className="flex min-h-11 min-w-0 items-center">
        <Brand className="h-9 max-w-[174px] object-left" />
      </NavLink>
      {mobile && <button type="button" aria-label="Close navigation" className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border border-field bg-control" onClick={close}><Icon name="close" /></button>}
    </div>
    <nav aria-label={mobile ? 'Mobile navigation' : 'Main navigation'} className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
      <SidebarSection title="Your media" items={navItems} onClick={close} />
      {showAdmin && <SidebarSection title="Manage" items={adminItems} onClick={close} />}
    </nav>
    <div className="mx-3 border-t border-border px-2 pb-5 pt-4">
      {session.data?.user ? <div className="flex min-w-0 items-center gap-3">
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full border border-border bg-control text-xs" aria-hidden="true">{Array.from(session.data.user.username)[0]?.toUpperCase()}</span>
        <div className="min-w-0 flex-1"><div className="truncate text-sm" title={session.data.user.username}>{session.data.user.username}</div><div className="mt-0.5 text-xs text-muted">{session.data.user.role === 'admin' ? 'Administrator' : 'Personal account'}</div></div>
        <a href="/logout" aria-label="Log out" title="Log out" className="flex min-h-11 w-11 shrink-0 items-center justify-center rounded-lg text-muted hover:bg-control hover:text-white"><Icon name="logout" /></a>
      </div> : session.isPending ? <p role="status" className="text-sm text-muted">Loading account...</p> : session.isError ? <p className="text-sm text-muted">Account unavailable</p> : <a href="/login" className="flex min-h-11 items-center text-sm text-muted hover:text-white">Sign in</a>}
      <div className="mt-4 text-[11px] text-muted">Requests and discovery</div>
    </div>
  </div>;

  return <div className="flex min-h-screen bg-bg text-[#f5f4f1]">
    <a href="#main-content" className="sr-only z-[500] rounded-lg bg-accent px-4 py-3 text-accent-ink focus:not-sr-only focus:fixed focus:left-4 focus:top-4">Skip to content</a>
    <aside className="sticky top-0 hidden h-dvh w-[232px] shrink-0 border-r border-border bg-card lg:block">{sidebar(false)}</aside>
    <dialog ref={drawer} className="navigation-dialog" aria-label="Navigation" onCancel={close} onClose={close} onClick={event => {
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) close();
    }}>{sidebar(true)}</dialog>
    <div className="flex min-w-0 flex-1 flex-col">
      <header className="sticky top-0 z-20 border-b border-border bg-bg/95 backdrop-blur-md">
        <div className="mx-auto flex min-h-[76px] max-w-[1680px] flex-wrap items-center gap-3 px-4 py-3 sm:px-6 lg:gap-6 lg:px-8">
          <button type="button" className="flex h-11 w-11 shrink-0 items-center justify-center rounded-lg border border-field bg-control lg:hidden" onClick={() => setDrawerOpen(true)} aria-label="Open navigation" aria-haspopup="dialog" aria-expanded={drawerOpen}><Icon name="menu" size={20} /></button>
          <nav aria-label="Breadcrumb" className="hidden min-w-0 items-center gap-3 text-xs text-muted sm:flex"><span>{appName}</span><Icon name="chevron-right" size={12} /><span className="whitespace-nowrap text-white">{routeTitle(location.pathname)}</span></nav>
          <div className="min-w-0 flex-1 sm:ml-auto sm:max-w-md"><TopbarSearch /></div>
          {session.data?.user && <RegionPicker region={session.data.user.region || 'NL'} />}
        </div>
      </header>
      <main id="main-content" tabIndex={-1} className="mx-auto w-full max-w-[1680px] min-w-0 flex-1 px-4 py-7 sm:px-6 lg:px-8 lg:py-8">
        <h1 className="sr-only">{routeTitle(location.pathname)}</h1>
        {session.isError && <div role="alert" className="mb-5 flex flex-wrap items-center gap-3 rounded-lg border border-danger/40 bg-card p-3 text-sm"><span>Could not load your account.</span><button type="button" onClick={() => session.refetch()} className="min-h-11 rounded-lg border border-field px-3">Try again</button></div>}
        <Outlet />
      </main>
    </div>
  </div>;
}

function TopbarSearch() {
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  return <form role="search" onSubmit={event => { event.preventDefault(); if (q.trim()) navigate(`/search?q=${encodeURIComponent(q.trim())}`); }}>
    <label className="relative block">
      <span className="sr-only">Search movies and series</span>
      <Icon name="search" size={17} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" />
      <input type="search" value={q} onChange={event => setQ(event.target.value)} placeholder="Search movies and series" className="min-h-11 w-full min-w-0 rounded-lg border border-field bg-control py-2 pl-10 pr-3 text-sm placeholder:text-muted" />
    </label>
  </form>;
}

function SidebarSection({ title, items, onClick }: { title: string; items: NavigationItem[]; onClick: () => void }) {
  return <section className="mb-6"><h2 className="mb-2 px-3 text-[10px] font-medium uppercase tracking-[0.12em] text-muted">{title}</h2>
    <div className="space-y-1">{items.map(item => <NavLink key={item.to} to={item.to} end={item.exact} onClick={onClick} className={({ isActive }) => `flex min-h-11 items-center gap-3 rounded-lg border px-3 text-[13px] transition-colors ${isActive ? 'border-white/15 bg-white/[0.075] text-white' : 'border-transparent text-muted hover:bg-control hover:text-white'}`}><Icon name={item.icon} size={18} /><span>{item.label}</span></NavLink>)}</div>
  </section>;
}

const REGIONS = [
  ['NL', 'Netherlands'], ['BE', 'Belgium'], ['ZA', 'South Africa'], ['US', 'United States'], ['GB', 'United Kingdom'],
  ['DE', 'Germany'], ['FR', 'France'], ['ES', 'Spain'], ['IT', 'Italy'], ['AU', 'Australia'], ['CA', 'Canada'],
  ['BR', 'Brazil'], ['IN', 'India'], ['JP', 'Japan'], ['KR', 'South Korea'], ['SE', 'Sweden'], ['NO', 'Norway'],
  ['DK', 'Denmark'], ['PT', 'Portugal'], ['PL', 'Poland'],
];

function RegionPicker({ region }: { region: string }) {
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: api.setRegion,
    onSuccess: (_data, code) => {
      queryClient.setQueryData<SessionInfo>(['session'], old => old?.user ? { ...old, user: { ...old.user, region: code } } : old);
      queryClient.invalidateQueries();
    },
  });
  return <div className="relative shrink-0">
    <label className="relative block"><span className="sr-only">Catalog region</span>
      <select value={region} onChange={event => { if (mutation.isPending) return; mutation.reset(); mutation.mutate(event.target.value); }} className="min-h-11 w-[72px] appearance-none rounded-lg border border-field bg-control py-2 pl-3 pr-6 text-transparent" aria-busy={mutation.isPending}>
        {!REGIONS.some(([code]) => code === region) && <option value={region} className="text-white">{region}</option>}
        {REGIONS.map(([code, name]) => <option key={code} value={code} className="text-white">{code} · {name}</option>)}
      </select><span aria-hidden="true" className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-xs font-medium">{region}</span><Icon name="chevron-down" size={12} className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-muted" />
    </label>
    {mutation.isError && <p role="alert" className="absolute right-0 top-full z-30 mt-2 w-60 max-w-[85vw] rounded-lg border border-danger/50 bg-card p-3 text-xs shadow-xl">Could not update the catalog region. Try again.</p>}
  </div>;
}
