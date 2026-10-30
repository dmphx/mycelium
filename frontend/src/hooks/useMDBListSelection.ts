import { useEffect, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { csrfToken } from '../api';
import type { useMDBListRead } from './readMDBList';

type Read = ReturnType<typeof useMDBListRead>;
const idsFrom = (value?: string) => (value || '').split(',').filter(Boolean);

async function writeSelection(ids: string[], signal: AbortSignal, current: () => boolean) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetch('/ui/api/mdblist/lists', {
      method: 'POST', signal: controller.signal, credentials: 'same-origin', redirect: 'error',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
      body: JSON.stringify({ list_ids: ids }),
    });
    if (controller.signal.aborted || !current()) throw new Error('Selection request ended');
    if (response.status === 401 && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
    if (!response.ok) throw new Error('Selection save unavailable');
    const body = await response.json();
    if (controller.signal.aborted || !current() || body?.ok !== true) throw new Error('Selection acknowledgement unavailable');
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}

export function useMDBListSelection(read: Read) {
  const qc = useQueryClient();
  const actor = read.actorId;
  const [selected, setSelected] = useState<string[]>([]);
  const [saving, setSaving] = useState(false), [refreshing, setRefreshing] = useState(false);
  const [unknown, setUnknown] = useState(false), [message, setMessage] = useState(''), [failed, setFailed] = useState(false);
  const [settledActor, setSettledActor] = useState(actor);
  const owner = useRef(actor), generation = useRef(0), mounted = useRef(false), busy = useRef(false);
  const request = useRef<AbortController | null>(null);
  if (owner.current !== actor) { owner.current = actor; generation.current += 1; }
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; generation.current += 1; request.current?.abort(); };
  }, []);
  useEffect(() => {
    request.current?.abort(); request.current = null; busy.current = false;
    setSettledActor(actor); setSaving(false); setRefreshing(false); setUnknown(false); setMessage(''); setFailed(false);
    setSelected(idsFrom(read.statusRead.data?.list_ids));
    // Reset only on actor changes; saved-state updates must not erase an unconfirmed draft.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actor]);
  useEffect(() => {
    if (!busy.current && !unknown) setSelected(idsFrom(read.statusRead.data?.list_ids));
  }, [actor, read.statusRead.data?.list_ids, unknown]);

  const toggle = async (id: string) => {
    if (!read.listsKnown || busy.current || unknown || actor === null || settledActor !== actor) return;
    busy.current = true;
    const sequence = ++generation.current, controller = new AbortController(); request.current = controller;
    const current = () => mounted.current && owner.current === actor && generation.current === sequence;
    const next = selected.includes(id) ? selected.filter(value => value !== id) : [...selected, id];
    setSelected(next); setSaving(true); setMessage(''); setFailed(false);
    try {
      await writeSelection(next, controller.signal, current);
      if (!current()) return;
      qc.setQueryData<NonNullable<Read['statusRead']['data']>>(['mdblist-status', actor], old =>
        old ? { ...old, list_ids: next.join(',') } : old);
      setUnknown(false); setMessage('Lists saved.');
    } catch {
      if (!current()) return;
      setUnknown(true); setFailed(true);
      setMessage('Could not confirm list selection was saved. Your requested selection is still shown. Refresh MDBList before changing lists or syncing.');
    } finally {
      if (current()) { busy.current = false; request.current = null; setSaving(false); }
    }
  };
  const refresh = async () => {
    if (!read.ready || busy.current || read.statusRead.isFetching || read.listsRead.isFetching || settledActor !== actor) return;
    busy.current = true;
    const sequence = ++generation.current;
    const current = () => mounted.current && owner.current === actor && generation.current === sequence;
    setRefreshing(true); setMessage(''); setFailed(false);
    try {
      const saved = await read.refresh();
      if (!current()) return;
      if (!saved) throw new Error('Saved selection unavailable');
      setSelected(idsFrom(saved.list_ids)); setUnknown(false); setMessage('Saved list selection refreshed.');
    } catch {
      if (!current()) return;
      setFailed(true); setMessage('Could not refresh saved list selection. The shown selection is not confirmed. Refresh MDBList to try again.');
    } finally {
      if (current()) { busy.current = false; setRefreshing(false); }
    }
  };
  return { selected, toggle, refresh, unknown, message, failed, saving, refreshing,
    pending: saving || refreshing, blocked: !read.listsKnown || saving || refreshing || unknown || settledActor !== actor };
}
