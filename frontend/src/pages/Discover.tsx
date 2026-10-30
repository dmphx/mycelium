import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { NL_PROVIDER_IDS, tmdbImg } from '../api';
import type { MediaType, TmdbItem } from '../types';
import PosterGrid from '../components/PosterGrid';
import DetailModal from '../components/DetailModal';
import SectionHeader from '../components/SectionHeader';
import { readDiscovery, readGenreTabs, readProviders } from '../hooks/readDiscovery';

export default function Discover() {
  const [detail, setDetail] = useState<{ id: number; type: MediaType } | null>(null);
  const [activeProvider, setActiveProvider] = useState<number | null>(null);
  const genreTabs = useCatalogQuery(['genre-tabs', 'discover'], readGenreTabs);

  const open = (item: TmdbItem) => setDetail({ id: item.tmdb_id, type: item.media_type });
  const close = () => setDetail(null);

  return (
    <div className="space-y-8">
      <ProviderStrip
        onPick={(pid) => { setActiveProvider(pid); setDetail(null); }}
        onItemClick={open}
      />

      {activeProvider === null && (
        <>
          <Row
            title="🔥 Trending this week"
            query={['trending', 'all', 'week']}
            fetcher={signal => readDiscovery('/ui/api/discover/trending?type=all&window=week', signal)}
            toggles={[
              { label: 'Today', queryKey: ['trending', 'all', 'day'], fetcher: signal => readDiscovery('/ui/api/discover/trending?type=all&window=day', signal) },
              { label: 'Week', queryKey: ['trending', 'all', 'week'], fetcher: signal => readDiscovery('/ui/api/discover/trending?type=all&window=week', signal) },
            ]}
            onItemClick={open}
          />

          <Row
            title="⭐ Popular movies"
            query={['popular', 'movie']}
            fetcher={signal => readDiscovery('/ui/api/discover/popular?type=movie', signal)}
            toggles={[
              { label: 'Movies', queryKey: ['popular', 'movie'], fetcher: signal => readDiscovery('/ui/api/discover/popular?type=movie', signal) },
              { label: 'TV', queryKey: ['popular', 'tv'], fetcher: signal => readDiscovery('/ui/api/discover/popular?type=tv', signal) },
            ]}
            onItemClick={open}
          />

          <Row
            title="🎬 Now playing in theaters"
            query={['now-playing']}
            fetcher={signal => readDiscovery('/ui/api/discover/now-playing', signal)}
            onItemClick={open}
          />

          <Row
            title="📅 Upcoming"
            query={['upcoming']}
            fetcher={signal => readDiscovery('/ui/api/discover/upcoming', signal)}
            onItemClick={open}
          />

          <Row
            title="🏆 Top rated"
            query={['top-rated', 'movie']}
            fetcher={signal => readDiscovery('/ui/api/discover/top-rated?type=movie', signal)}
            toggles={[
              { label: 'Movies', queryKey: ['top-rated', 'movie'], fetcher: signal => readDiscovery('/ui/api/discover/top-rated?type=movie', signal) },
              { label: 'TV', queryKey: ['top-rated', 'tv'], fetcher: signal => readDiscovery('/ui/api/discover/top-rated?type=tv', signal) },
            ]}
            onItemClick={open}
          />

          <section aria-label="Your genres">
            <SectionHeader title="Your genres" />
            <ReadStatus label="genre sections" pending={genreTabs.isFetching} failed={genreTabs.isError} hasData={genreTabs.data !== undefined} refresh={genreTabs.refresh} />
            {genreTabs.isPending && !genreTabs.isError && <p role="status" className="text-sm text-muted">Loading genre sections...</p>}
            {genreTabs.data?.length === 0 && <p className="text-sm text-muted">No genre sections configured.</p>}
          </section>
          {(genreTabs.data || []).map((tab) => (
            <Row
              key={`genre-${tab.media_type}-${tab.genre_id}-${tab.year_from ?? ''}-${tab.year_to ?? ''}`}
              title={`🎭 ${tab.genre_name}${tab.year_from || tab.year_to ? ` (${tab.year_from ?? ''}${tab.year_from || tab.year_to ? '–' : ''}${tab.year_to ?? ''})` : ''}`}
              query={['genre', tab.media_type, tab.genre_id, tab.year_from, tab.year_to]}
              fetcher={signal => readDiscovery(`/ui/api/discover/by-genre?type=${tab.media_type}&genre_id=${tab.genre_id}${tab.year_from ? `&year_from=${tab.year_from}` : ''}${tab.year_to ? `&year_to=${tab.year_to}` : ''}`, signal)}
              onItemClick={open}
            />
          ))}
        </>
      )}

      <DetailModal
        tmdbId={detail?.id ?? null}
        mediaType={detail?.type ?? null}
        onClose={close}
        onSelectItem={open}
      />
    </div>
  );
}

interface ToggleSpec {
  label: string;
  queryKey: readonly unknown[];
  fetcher: (signal: AbortSignal) => Promise<TmdbItem[]>;
}

function Row({
  title,
  query,
  fetcher,
  toggles,
  onItemClick,
}: {
  title: string;
  query: readonly unknown[];
  fetcher: (signal: AbortSignal) => Promise<TmdbItem[]>;
  toggles?: ToggleSpec[];
  onItemClick: (item: TmdbItem) => void;
}) {
  const [active, setActive] = useState<{ key: readonly unknown[]; fn: (signal: AbortSignal) => Promise<TmdbItem[]> }>(
    { key: query, fn: fetcher },
  );
  const queryState = useCatalogQuery(['discover-row', ...active.key], active.fn);

  return (
    <section aria-label={title}>
      <SectionHeader
        title={title}
        action={
          toggles &&
          toggles.map((t) => (
            <button
              key={t.label}
              type="button"
              onClick={() => setActive({ key: t.queryKey, fn: t.fetcher })}
              aria-pressed={JSON.stringify(active.key) === JSON.stringify(t.queryKey)}
              className={`text-xs px-3 py-1 rounded border ${
                JSON.stringify(active.key) === JSON.stringify(t.queryKey)
                  ? 'border-accent bg-accent/10 text-white'
                  : 'border-border text-muted hover:text-white'
              }`}
            >
              {t.label}
            </button>
          ))
        }
      />
      <ReadStatus label={title} pending={queryState.isFetching} failed={queryState.isError} hasData={queryState.data !== undefined} refresh={queryState.refresh} />
      {queryState.isPending && !queryState.isError && <p role="status" className="text-sm text-muted">Loading this section...</p>}
      {queryState.data !== undefined && <PosterGrid items={queryState.data} onItemClick={onItemClick} />}
    </section>
  );
}

function ProviderStrip({ onPick, onItemClick }: { onPick: (pid: number | null) => void; onItemClick: (item: TmdbItem) => void }) {
  const [activePid, setActivePid] = useState<number | null>(null);
  const providers = useCatalogQuery(['discover-providers', 'movie'], readProviders);

  const wanted = Object.values(NL_PROVIDER_IDS);
  const visible = (providers.data || [])
    .filter((p) => wanted.includes(p.id as any))
    .sort((a, b) => wanted.indexOf(a.id as any) - wanted.indexOf(b.id as any));

  const activeName = visible.find((p) => p.id === activePid)?.name || '';

  return (
    <section>
      <div className="flex gap-2 overflow-x-auto scrollbar-hidden pb-2 -mx-1 px-1">
        <ProviderChip
          name="All"
          active={activePid === null}
          onClick={() => {
            setActivePid(null);
            onPick(null);
          }}
        />
        {visible.map((p) => (
          <ProviderChip
            key={p.id}
            name={p.name}
            logo={tmdbImg.logo(p.logo_path)}
            active={activePid === p.id}
            onClick={() => {
              setActivePid(p.id);
              onPick(p.id);
            }}
          />
        ))}
      </div>
      <ReadStatus label="providers" pending={providers.isFetching} failed={providers.isError} hasData={providers.data !== undefined} refresh={providers.refresh} />
      {providers.isPending && !providers.isError && <p role="status" className="text-sm text-muted">Loading providers...</p>}
      {providers.data?.length === 0 && <p className="text-sm text-muted">No streaming providers available.</p>}
      {activePid !== null && (
        <div className="space-y-8 mt-6">
          <ProviderRow
            title={`Trending ${activeName} movies`}
            pid={activePid}
            type="movie"
            sortBy="popularity.desc"
            onItemClick={onItemClick}
          />
          <ProviderRow
            title={`Trending ${activeName} series`}
            pid={activePid}
            type="tv"
            sortBy="popularity.desc"
            onItemClick={onItemClick}
          />
          <ProviderRow
            title={`Top rated ${activeName} movies`}
            pid={activePid}
            type="movie"
            sortBy="vote_average.desc"
            onItemClick={onItemClick}
          />
          <ProviderRow
            title={`Top rated ${activeName} series`}
            pid={activePid}
            type="tv"
            sortBy="vote_average.desc"
            onItemClick={onItemClick}
          />
        </div>
      )}
    </section>
  );
}

function ProviderRow({ title, pid, type, sortBy, onItemClick }: {
  title: string; pid: number; type: MediaType; sortBy: string; onItemClick: (item: TmdbItem) => void;
}) {
  const queryState = useCatalogQuery(['discover-provider-row', pid, type, sortBy],
    signal => readDiscovery(`/ui/api/discover/by-provider?type=${type}&provider_id=${pid}&sort_by=${sortBy}`, signal));
  return (
    <section aria-label={title}>
      <SectionHeader title={title} />
      <ReadStatus label={title} pending={queryState.isFetching} failed={queryState.isError} hasData={queryState.data !== undefined} refresh={queryState.refresh} />
      {queryState.isPending && !queryState.isError && <p role="status" className="text-sm text-muted">Loading this section...</p>}
      {queryState.data !== undefined && <PosterGrid items={queryState.data} onItemClick={onItemClick} />}
    </section>
  );
}

function ProviderChip({
  name,
  logo,
  active,
  onClick,
}: {
  name: string;
  logo?: string | null;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`flex-shrink-0 flex items-center gap-2 px-3 py-2 rounded-full text-xs whitespace-nowrap
                   border transition ${
                     active
                       ? 'bg-accent text-white border-accent'
                       : 'bg-card text-white border-border hover:border-accent/50'
                   }`}
    >
      {logo && <img src={logo} alt="" className="w-5 h-5 rounded" />}
      <span>{name}</span>
    </button>
  );
}

function useCatalogQuery<T>(key: readonly unknown[], read: (signal: AbortSignal) => Promise<T>) {
  const inFlight = useRef(false);
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => read(signal), retry: false });
  const refresh = async () => {
    if (inFlight.current || query.isFetching) return;
    inFlight.current = true;
    try { await query.refetch({ cancelRefetch: false }); }
    finally { inFlight.current = false; }
  };
  return { ...query, refresh };
}

function ReadStatus({ label, pending, failed, hasData, refresh }: {
  label: string; pending: boolean; failed: boolean; hasData: boolean; refresh: () => void;
}) {
  return <div className="mb-3 space-y-2">
    {failed && <p role="alert" className="text-sm text-amber-300">
      {hasData ? 'Could not refresh this section. Previous results are still shown.' : 'Could not load this section.'}
    </p>}
    <button type="button" aria-label={`Refresh ${label}`} disabled={pending} aria-busy={pending}
      onClick={refresh} className="min-h-11 rounded-lg border border-border px-3 text-xs disabled:opacity-50">
      {pending ? 'Loading...' : 'Refresh'}
    </button>
  </div>;
}
