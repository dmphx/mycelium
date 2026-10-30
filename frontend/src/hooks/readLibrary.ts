import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';

type Kind = 'movies' | 'series';
type LibraryData = { items?: any[]; series?: any[] };
const optionalText = (value: unknown) => value == null || typeof value === 'string';
const ordinal = (value: unknown) => Number.isSafeInteger(value) && Number(value) >= 0;

// Native response shape only. No provider calls, writes or playback effects.
async function readLibrary(kind: Kind, signal: AbortSignal): Promise<LibraryData> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetch(`/ui/api/library/${kind === 'movies' ? 'movies' : 'series-episodes'}`, {
      signal: controller.signal, credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/json' },
    });
    if (controller.signal.aborted) throw new Error('Library read ended');
    if (response.status === 401 && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
    if (!response.ok) throw new Error('Library unavailable');
    const body = await response.json();
    if (controller.signal.aborted) throw new Error('Library read ended');
    const rows = body?.[kind === 'movies' ? 'items' : 'series'];
    if (!Array.isArray(rows) || rows.some(row => {
      if (!row || typeof row.title !== 'string' || !optionalText(row.imdb_id)) return true;
      if (kind === 'movies') return !optionalText(row.poster_path) || !optionalText(row.status);
      return !Array.isArray(row.seasons) || row.seasons.some((season: any) => !season || !ordinal(season.season)
        || !(season.year == null || typeof season.year === 'string' || typeof season.year === 'number')
        || !Array.isArray(season.episodes) || !season.episodes.every(ordinal))
        || (row.missing != null && (!Array.isArray(row.missing) || row.missing.some((episode: any) =>
          !episode || !ordinal(episode.season) || !ordinal(episode.episode))));
    })) throw new Error('Invalid library response');
    return body;
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}

export function useLibraryRead(kind: Kind) {
  const query = useQuery<LibraryData>({
    queryKey: [kind === 'movies' ? 'library-movies' : 'library-series-episodes'],
    queryFn: ({ signal }) => readLibrary(kind, signal), retry: false,
  });
  const busy = useRef(false);
  const mounted = useRef(false);
  const [refreshed, setRefreshed] = useState(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const refresh = useCallback(async () => {
    if (busy.current || query.isFetching) return;
    busy.current = true;
    setRefreshed(false);
    try {
      const result = await query.refetch();
      if (mounted.current) setRefreshed(result.isSuccess);
    } finally { busy.current = false; }
  }, [query.isFetching, query.refetch]);
  return { ...query, refresh, refreshed };
}
