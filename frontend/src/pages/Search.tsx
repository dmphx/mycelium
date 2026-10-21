import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';
import type { MediaType, TmdbItem } from '../types';
import PosterCard from '../components/PosterCard';
import DetailModal from '../components/DetailModal';

export default function Search() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [q, setQ] = useState(searchParams.get('q') || '');
  const selectedType = searchParams.get('type');
  const typeFilter = selectedType === 'movie' || selectedType === 'tv' ? selectedType : 'all';

  // Pick up ?q= changes from the topbar search bar (or a shared link) after mount.
  useEffect(() => {
    const urlQ = searchParams.get('q') || '';
    if (urlQ !== q) setQ(urlQ);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const updateQuery = (value: string) => {
    setQ(value);
    setSearchParams({ ...(value ? { q: value } : {}), ...(typeFilter !== 'all' ? { type: typeFilter } : {}) }, { replace: true });
  };
  const [detail, setDetail] = useState<{ id: number; type: MediaType } | null>(null);

  // Debounce the value that actually triggers a search request so fast typing
  // doesn't fire one TMDB call per keystroke - the input itself stays instant.
  const [debouncedQ, setDebouncedQ] = useState(q);
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQ(q), 300);
    return () => clearTimeout(timer);
  }, [q]);

  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['search', debouncedQ],
    queryFn: () => api.search(debouncedQ).then((r) => r.results),
    enabled: debouncedQ.trim().length > 0,
  });

  const filtered = (data || []).filter((i) =>
    typeFilter === 'all' ? true : i.media_type === typeFilter,
  );

  const open = (it: TmdbItem) => setDetail({ id: it.tmdb_id, type: it.media_type });
  const waiting = q !== debouncedQ || isLoading;
  const filter = (type: 'all' | MediaType) => setSearchParams({
    ...(q ? { q } : {}), ...(type !== 'all' ? { type } : {}),
  });

  return (
    <div className="min-w-0 space-y-5">
      <div className="space-y-4">
        <label className="block max-w-2xl">
          <span className="sr-only">Search the catalog</span>
          <input
            type="search"
            autoFocus
            value={q}
            onChange={(e) => updateQuery(e.target.value)}
            placeholder="Search movies and series"
            className="min-h-11 w-full bg-control border border-field rounded-xl px-4 py-3 text-sm
                       text-white placeholder-muted"
          />
        </label>
        <div role="group" aria-label="Filter search results" className="flex flex-wrap gap-2 pb-4 border-b border-border">
          {([['all', 'All titles'], ['movie', 'Movies'], ['tv', 'Series']] as const).map(([type, label]) => (
            <button key={type} type="button" aria-pressed={typeFilter === type}
              onClick={() => filter(type)}
              className={`min-h-11 rounded-xl border px-4 py-2 text-sm transition-colors ${typeFilter === type
                ? 'bg-accent text-accent-ink border-accent'
                : 'bg-control text-muted border-field hover:text-white hover:bg-raised'}`}>
              {label}
            </button>
          ))}
        </div>
      </div>
      {q.trim() && !waiting && !isError && (
        <p className="text-muted text-sm break-words" role="status">
          {filtered.length} {filtered.length === 1 ? 'result' : 'results'} for &quot;{debouncedQ}&quot;
          {isFetching && <span className="ml-2">Updating…</span>}
        </p>
      )}
      {q.trim() ? (
        waiting ? (
          <div className="text-muted text-sm py-8" role="status">Searching the catalog…</div>
        ) : isError ? (
          <div role="alert" className="rounded-xl border border-border bg-card p-5 space-y-3">
            <p className="text-sm">Search is unavailable. Your search has been kept.</p>
            <button type="button" onClick={() => void refetch()} disabled={isFetching}
              className="min-h-11 rounded-xl border border-field bg-control px-4 py-2 text-sm disabled:opacity-50">
              {isFetching ? 'Trying again…' : 'Try search again'}
            </button>
          </div>
        ) : filtered.length > 0 ? (
          <ul aria-label="Search results" className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 2xl:grid-cols-6 gap-3 lg:gap-4">
            {filtered.map((it) => (
              <li key={`${it.media_type}-${it.tmdb_id}`} className="min-w-0">
                <PosterCard item={it} onClick={open} status={it.library_status} />
              </li>
            ))}
          </ul>
        ) : (
          <div className="rounded-xl border border-border bg-card py-12 px-5 text-center space-y-2">
            <p className="font-semibold">No matching titles</p>
            <p className="text-muted text-sm">Try another search or choose a different filter.</p>
          </div>
        )
      ) : (
        <div className="text-muted text-sm py-8 text-center">
          Start typing to search across movies and series.
        </div>
      )}
      <DetailModal
        tmdbId={detail?.id ?? null}
        mediaType={detail?.type ?? null}
        onClose={() => setDetail(null)}
        onSelectItem={open}
      />
    </div>
  );
}
