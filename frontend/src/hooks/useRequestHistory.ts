import { parseNativeDispatch, type NativeDispatch } from '../components/RequestDispatchStatus';
import { useQuery } from '@tanstack/react-query';

export type RequestHistoryRow = {
  id: number; user_id: number; title: string; media_type: 'movie' | 'tv';
  imdb_id?: string | null; tmdb_id?: number | null;
  request_dispatch: NativeDispatch;
  status: string; library_status?: string | null; created_at?: string | null;
};

function historyRows(body: unknown, actorId: number): RequestHistoryRow[] {
  if (!body || typeof body !== 'object' || !('items' in body) || !Array.isArray(body.items)
    || body.items.length > 200) throw new Error('Invalid history response');
  const ids = new Set<number>();
  for (const row of body.items) {
    if (!row || typeof row !== 'object' || !Number.isSafeInteger(row.id) || row.id <= 0
      || row.user_id !== actorId || ids.has(row.id)
      || typeof row.title !== 'string' || !row.title.trim()
      || !['movie', 'tv'].includes(row.media_type)
      || typeof row.status !== 'string' || !row.status
      || (row.library_status != null && typeof row.library_status !== 'string')
      || (row.created_at != null && typeof row.created_at !== 'string')) throw new Error('Invalid history row');
    ids.add(row.id);
  }
  return body.items.map(row => ({ ...row, request_dispatch: parseNativeDispatch(row.request_dispatch) }));
}

export async function readRequestHistory(actorId: number, signal: AbortSignal) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetch('/ui/api/user-requests?mine=1', {
      signal: controller.signal, credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/json' },
    });
    if (controller.signal.aborted) throw new Error('History read ended');
    if (response.status === 401 && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
    if (!response.ok) throw new Error('History unavailable');
    const body: unknown = await response.json();
    if (controller.signal.aborted) throw new Error('History read ended');
    return { items: historyRows(body, actorId) };
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}

// The backend owns authorization. This cache is separated by the existing native
// account and never treats malformed data or an absent row as non-admission.
export function useRequestHistory(actorId: number | null) {
  return useQuery({
    queryKey: ['my-requests', actorId],
    queryFn: ({ signal }) => {
      if (actorId === null) throw new Error('Account unavailable');
      return readRequestHistory(actorId, signal);
    },
    enabled: actorId !== null, retry: false, staleTime: 0,
  });
}
