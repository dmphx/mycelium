import { useCallback, useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';

type Status = { connected: boolean; list_ids: string };
type Lists = { lists: Array<{ id: number | string; name: string }> };
async function read<T>(kind: 'status' | 'lists', signal: AbortSignal): Promise<T> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 15000);
  try {
    const response = await fetch(`/ui/api/mdblist/${kind}`, {
      signal: controller.signal, credentials: 'same-origin', redirect: 'error', headers: { Accept: 'application/json' },
    });
    if (controller.signal.aborted) throw new Error('MDBList read ended');
    if (response.status === 401 && !window.location.pathname.endsWith('/login')) window.location.href = '/login';
    if (!response.ok) throw new Error('MDBList read unavailable');
    const body = await response.json();
    if (controller.signal.aborted) throw new Error('MDBList read ended');
    if (!body || (kind === 'status'
      ? typeof body.connected !== 'boolean' || typeof body.list_ids !== 'string'
      : !Array.isArray(body.lists) || body.lists.some((list: any) => !list || typeof list.name !== 'string'
        || !((typeof list.id === 'string' && list.id.length > 0) || (Number.isSafeInteger(list.id) && list.id >= 0)))))
      throw new Error('Invalid MDBList response');
    return body;
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}

export function useMDBListRead() {
  const account = useQuery({ queryKey: ['session'], queryFn: api.session });
  const actorId = account.data?.authenticated ? account.data.user?.id ?? null : null;
  const ready = actorId !== null;
  const statusRead = useQuery({ queryKey: ['mdblist-status', actorId], enabled: ready,
    queryFn: ({ signal }) => read<Status>('status', signal), retry: false });
  const listsRead = useQuery({ queryKey: ['mdblist-lists', actorId], enabled: ready && statusRead.data?.connected === true && !statusRead.isError,
    queryFn: ({ signal }) => read<Lists>('lists', signal), retry: false });
  const busy = useRef(false), mounted = useRef(false), owner = useRef(actorId);
  owner.current = actorId;
  const [refreshed, setRefreshed] = useState(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => { setRefreshed(false); }, [actorId]);
  const refresh = useCallback(async () => {
    if (!ready || busy.current || statusRead.isFetching || listsRead.isFetching) return;
    busy.current = true; setRefreshed(false);
    try {
      const result = await statusRead.refetch({ cancelRefetch: false });
      if (!mounted.current || owner.current !== actorId || !result.isSuccess) return;
      if (result.data.connected) {
        const lists = await listsRead.refetch({ cancelRefetch: false });
        if (!lists.isSuccess) return;
      }
      if (mounted.current && owner.current === actorId) { setRefreshed(true); return result.data; }
    } finally { busy.current = false; }
  }, [actorId, ready, statusRead.isFetching, listsRead.isFetching, statusRead.refetch, listsRead.refetch]);
  const statusKnown = ready && !!statusRead.data && !statusRead.isError && !statusRead.isFetching;
  const listsKnown = statusKnown && !!listsRead.data && !listsRead.isError && !listsRead.isFetching;
  return { account, actorId, ready, statusRead, listsRead, statusKnown, listsKnown, refresh, refreshed };
}
