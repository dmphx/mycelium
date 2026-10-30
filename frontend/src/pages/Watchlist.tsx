import { useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { readWatchlist } from '../hooks/readWatchlist';
import type { MediaType, TmdbItem } from '../types';
import PosterCard from '../components/PosterCard';
import DetailModal from '../components/DetailModal';

export default function Watchlist() {
  const [detail, setDetail] = useState<{ id: number; type: MediaType } | null>(null);
  const refreshInFlight = useRef(false);
  const { data, isPending, isFetching, isError, refetch } = useQuery({
    queryKey: ['watchlist'], queryFn: ({ signal }) => readWatchlist(signal), retry: false,
  });
  const refresh = async () => {
    if (refreshInFlight.current || isFetching) return;
    refreshInFlight.current = true;
    try { await refetch({ cancelRefetch: false }); }
    finally { refreshInFlight.current = false; }
  };

  const open = (item: TmdbItem) => setDetail({ id: item.tmdb_id, type: item.media_type });

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
        {data && <p className="text-muted text-sm">{data.items.length} items in your watchlist</p>}
        <button type="button" onClick={refresh} disabled={isFetching} aria-busy={isFetching}
          className="min-h-11 rounded-lg border border-border px-4 text-sm disabled:opacity-50">
          {isFetching ? 'Refreshing watchlist...' : 'Refresh watchlist'}
        </button>
      </div>
      {isPending && !isError && <p role="status" className="text-muted text-sm py-6">Loading your watchlist...</p>}
      {isError && <p role="alert" className="text-sm text-amber-300 mb-4">
        {data ? 'Could not refresh your watchlist. Previous results are still shown.' : 'Could not load your watchlist.'}
      </p>}
      {data?.items.length === 0 && <div className="text-center py-16">
        <div className="text-5xl mb-3">&#9733;</div>
        <h2 className="text-lg font-semibold mb-1">Your watchlist is empty</h2>
        <p className="text-muted text-sm">Add items from the Discover page to track what you want to watch.</p>
      </div>}
      <div aria-label="Watchlist results" className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 200px))' }}>
        {data?.items.map((it) => (
          <PosterCard
            key={it.id}
            item={{
              tmdb_id: it.tmdb_id || 0,
              media_type: it.media_type,
              title: it.title,
              year: null,
              rating: 0,
              votes: 0,
              popularity: 0,
              overview: '',
              poster_path: it.poster_path,
              backdrop_path: null,
            }}
            onClick={open}
            status={it.library_status}
          />
        ))}
      </div>
      <DetailModal
        tmdbId={detail?.id ?? null}
        mediaType={detail?.type ?? null}
        onClose={() => setDetail(null)}
        onSelectItem={open}
      />
    </div>
  );
}
