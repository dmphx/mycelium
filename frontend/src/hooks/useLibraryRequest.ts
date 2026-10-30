import { parseNativeDispatch, type NativeDispatch } from '../components/RequestDispatchStatus';
import { useEffect, useRef, useState } from 'react';
import { useQuery, useQueryClient, type QueryClient } from '@tanstack/react-query';
import { csrfToken } from '../api';
import { readRequestHistory } from './useRequestHistory';
import type { MediaType } from '../types';

type Phase = 'idle' | 'submitting' | 'pending' | 'approved' | 'checking_approval' | 'approval_error' | 'denied' | 'processing' | 'registered' | 'wanted' | 'upcoming' | 'failed' | 'unknown' | 'status_error';
type Selection = Readonly<{ mode: 'all' | 'future' | 'selected'; seasons: readonly number[] }>;
type Attempt = { phase: Phase; revision: number; imdb?: string; requestId?: number; deadline?: number; selection?: Selection; dispatch?: NativeDispatch };
type Input = { tmdb_id: number; media_type: MediaType; title: string; monitor_mode?: 'all' | 'future' | 'selected'; seasons?: number[] };
const IDLE: Attempt = { phase: 'idle', revision: 0 };
const MAX_ATTEMPTS = 128;
// Browser-memory uncertainty is retained across modal close/reopen, per native
// actor and typed title. It is not a durable server idempotency or admission gate.
const attempts = new WeakMap<QueryClient, Map<string, Attempt>>();

class HttpFailure extends Error {
  constructor(readonly status: number) { super(`HTTP ${status}`); }
}

async function json(url: string, signal: AbortSignal, init: RequestInit = {}, timeout = 15000) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, timeout);
  try {
    const response = await fetch(url, { ...init, signal: controller.signal, credentials: 'same-origin', redirect: 'error' });
    if (!response.ok) throw new HttpFailure(response.status);
    return await response.json();
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}

export function useLibraryRequest(actor: string | null, mediaType: MediaType | null, tmdbId: number | null) {
  const client = useQueryClient();
  if (!attempts.has(client)) attempts.set(client, new Map());
  const ledger = attempts.get(client)!;
  const key = ['library-request-ui', actor, mediaType, tmdbId];
  const identity = JSON.stringify(key);
  const { data = IDLE } = useQuery<Attempt>({
    queryKey: key, queryFn: async () => IDLE, enabled: false,
    initialData: () => ledger.get(identity) || IDLE,
  });
  const lifetime = useRef<AbortController | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const store = (value: Attempt) => {
    ledger.set(identity, value);
    client.setQueryData(key, value);
  };
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    setUnavailable(false);
    return () => controller.abort();
  }, [identity]);

  useEffect(() => {
    if (data.phase !== 'processing' || !data.imdb || !data.deadline) return;
    const controller = new AbortController();
    const active = () => !controller.signal.aborted && ledger.get(identity)?.revision === data.revision
      && ledger.get(identity)?.phase === 'processing';
    let next: ReturnType<typeof setTimeout>;
    const expired = setTimeout(() => {
      if (active()) store({ ...data, phase: 'status_error' });
      controller.abort();
    }, Math.max(0, data.deadline - Date.now()));
    const check = async () => {
      try {
        const result = await json(`/ui/api/requests/status?imdb_id=${encodeURIComponent(data.imdb!)}`, controller.signal);
        if (!active()) return;
        const phase: Partial<Record<string, Phase>> = { success: 'registered', wanted: 'wanted', upcoming: 'upcoming', failed: 'failed', rate_limited: 'failed' };
        if (!result || typeof result.status !== 'string') throw new Error('Invalid status');
        if (Object.prototype.hasOwnProperty.call(phase, result.status)) {
          store({ ...data, phase: phase[result.status]! });
          if (result.status === 'success') void client.invalidateQueries({ queryKey: ['detail', mediaType, tmdbId] });
        } else if (['pending', 'processing', 'not_found', 'unknown'].includes(result.status)) {
          next = setTimeout(check, 1000);
        } else throw new Error('Invalid status');
      } catch (error) {
        if (!active()) return;
        store({ ...data, phase: 'status_error' });
        if (error instanceof HttpFailure && error.status === 401 && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
      }
    };
    next = setTimeout(check, 1000);
    return () => { controller.abort(); clearTimeout(next); clearTimeout(expired); };
  }, [identity, data.phase, data.revision]);

  const submit = async (input: Input, expectedImdb?: string) => {
    // Claim synchronously before awaiting fetch, including same-turn double clicks.
    if (!actor || !mediaType || !tmdbId || ledger.has(identity) || !lifetime.current || lifetime.current.signal.aborted) return;
    if (input.tmdb_id !== tmdbId || input.media_type !== mediaType || typeof input.title !== 'string' || !input.title.trim()) {
      setUnavailable(true); return;
    }
    if (ledger.size >= MAX_ATTEMPTS) { setUnavailable(true); return; }
    if (mediaType === 'tv' && (!['all', 'future', 'selected'].includes(input.monitor_mode || '')
      || !Array.isArray(input.seasons) || input.seasons.some(n => !Number.isSafeInteger(n) || n < 1)
      || new Set(input.seasons).size !== input.seasons.length
      || (input.monitor_mode === 'selected' && !input.seasons.length))) { setUnavailable(true); return; }
    const controller = lifetime.current;
    // Preserve exactly the click-time selection, even if the response is lost or
    // later title metadata no longer lists those seasons. This is not scope proof.
    const selection: Selection | undefined = mediaType === 'tv'
      ? Object.freeze({ mode: input.monitor_mode!, seasons: Object.freeze([...input.seasons!]) }) : undefined;
    const pending: Attempt = { phase: 'submitting', revision: 1, selection };
    store(pending);
    try {
      const receipt = await json('/ui/api/discover/add', controller.signal, {
        method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() }, body: JSON.stringify(input),
      });
      if (controller.signal.aborted) throw new Error('Closed attempt');
      if (!receipt || !['pending', 'approved', 'queued'].includes(receipt.status)
        || typeof receipt.imdb_id !== 'string' || !/^tt[0-9]{7,10}$/.test(receipt.imdb_id)
        || (expectedImdb && receipt.imdb_id !== expectedImdb)
        || (receipt.status !== 'queued' && (!Number.isSafeInteger(receipt.request_id) || receipt.request_id <= 0))) throw new Error('Unconfirmed receipt');
      store({ ...pending, phase: receipt.status === 'pending' ? 'pending' : receipt.status === 'approved' ? 'approved' : 'processing',
        imdb: receipt.imdb_id, requestId: receipt.request_id, dispatch: parseNativeDispatch(receipt.request_dispatch),
        deadline: receipt.status === 'queued' ? Date.now() + 180000 : undefined });
      void client.invalidateQueries({ queryKey: ['my-requests'] });
    } catch (error) {
      store({ ...pending, phase: 'unknown' });
      if (!controller.signal.aborted && error instanceof HttpFailure && error.status === 401
        && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
    }
  };
  const refreshApproval = async () => {
    const current = ledger.get(identity);
    const controller = lifetime.current;
    const owner = actor !== null && /^(0|[1-9][0-9]*)$/.test(actor) ? Number(actor) : NaN;
    if (!current || !['pending', 'approved', 'approval_error'].includes(current.phase) || !current.requestId
      || !current.imdb || !controller || controller.signal.aborted || !Number.isSafeInteger(owner)) return;
    const checking = { ...current, phase: 'checking_approval' as const, revision: current.revision + 1 };
    store(checking);
    const active = () => ledger.get(identity)?.revision === checking.revision
      && ledger.get(identity)?.phase === 'checking_approval';
    try {
      const history = await readRequestHistory(owner, controller.signal);
      if (controller.signal.aborted) throw new Error('Closed approval read');
      if (!active()) return;
      const row = history.items.find(item => item.id === current.requestId);
      if (!row || row.media_type !== mediaType || row.tmdb_id !== tmdbId || row.imdb_id !== current.imdb
        || !['pending', 'approved', 'denied'].includes(row.status)) throw new Error('Unconfirmed owned approval');
      store({ ...checking, phase: row.status === 'approved' ? 'approved' : row.status === 'denied' ? 'denied' : 'pending',
        dispatch: row.request_dispatch, deadline: undefined });
    } catch {
      // A missing row in the bounded recent list never proves non-admission.
      // Keep the accepted request identity and submitted selection after close.
      if (active()) store({ ...checking, phase: 'approval_error' });
    }
  };
  const refreshStatus = () => {
    const current = ledger.get(identity);
    if (current?.imdb && ['status_error', 'failed', 'wanted', 'upcoming'].includes(current.phase))
      store({ ...current, phase: 'processing', revision: current.revision + 1, deadline: Date.now() + 180000 });
  };
  return { attempt: data, submit, refreshStatus, refreshApproval, unavailable, canSubmit: !!actor && data.phase === 'idle' && !unavailable };
}
