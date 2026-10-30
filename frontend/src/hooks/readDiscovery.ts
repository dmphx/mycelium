import type { TmdbItem, Provider } from '../types';
import type { GenreRule } from '../api';

async function read<T>(url: string, field: string, signal: AbortSignal, valid: (row: any) => boolean): Promise<T[]> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetch(url, {
      signal: controller.signal, credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/json' },
    });
    if (controller.signal.aborted) throw new Error('Discovery read ended');
    if (response.status === 401 && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
    if (!response.ok) throw new Error('Discovery unavailable');
    const body = await response.json();
    if (controller.signal.aborted) throw new Error('Discovery read ended');
    if (!body || !Array.isArray(body[field]) || body[field].some((row: unknown) => !row || !valid(row)))
      throw new Error('Invalid discovery response');
    return body[field];
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}

export const readDiscovery = (url: string, signal: AbortSignal) => read<TmdbItem>(url, 'results', signal, row =>
  Number.isSafeInteger(row.tmdb_id) && row.tmdb_id > 0 && ['movie', 'tv'].includes(row.media_type)
  && typeof row.title === 'string' && (row.poster_path == null || typeof row.poster_path === 'string'));

export const readProviders = (signal: AbortSignal) => read<Provider>('/ui/api/discover/providers?type=movie', 'providers', signal, row =>
  Number.isSafeInteger(row.id) && row.id > 0 && typeof row.name === 'string'
  && (row.logo_path == null || typeof row.logo_path === 'string'));

export const readGenreTabs = (signal: AbortSignal) => read<GenreRule>('/ui/api/discover/genre-tabs', 'tabs', signal, row =>
  Number.isSafeInteger(row.genre_id) && row.genre_id > 0 && ['movie', 'tv'].includes(row.media_type)
  && typeof row.genre_name === 'string'
  && ['year_from', 'year_to'].every(key => row[key] == null || Number.isSafeInteger(row[key])));
