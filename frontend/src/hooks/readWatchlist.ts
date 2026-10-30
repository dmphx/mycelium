import type { WatchlistItem } from '../types';

// Native watchlist reads only. Cancellation must win over a late login response.
export async function readWatchlist(signal: AbortSignal): Promise<{ items: WatchlistItem[] }> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetch('/ui/api/watchlist', {
      signal: controller.signal, credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/json' },
    });
    if (controller.signal.aborted) throw new Error('Watchlist read ended');
    if (response.status === 401 && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
    if (!response.ok) throw new Error('Watchlist unavailable');
    const body = await response.json();
    if (controller.signal.aborted) throw new Error('Watchlist read ended');
    if (!body || !Array.isArray(body.items) || body.items.some((row: WatchlistItem) =>
      !row || typeof row.title !== 'string' || !Number.isSafeInteger(row.id)
      || !['movie', 'tv'].includes(row.media_type)
      || (row.tmdb_id !== null && (!Number.isSafeInteger(row.tmdb_id) || row.tmdb_id <= 0))
      || (row.poster_path !== null && typeof row.poster_path !== 'string')
    )) throw new Error('Invalid watchlist response');
    return body;
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}
