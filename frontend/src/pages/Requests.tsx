import RequestDispatchStatus from '../components/RequestDispatchStatus';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import Icon from '../components/Icon';
import { useRequestHistory } from '../hooks/useRequestHistory';

const controlStyle = 'min-h-11 px-4 py-2 rounded-xl border border-field bg-control text-sm hover:bg-raised disabled:opacity-50';

export default function Requests() {
  const { data: sessionData, isPending: accountLoading, isError: accountError } = useQuery({ queryKey: ['session'], queryFn: api.session });
  const isAdmin = sessionData?.user?.role === 'admin';
  const id = sessionData?.user?.id;
  const actorId = sessionData?.authenticated && Number.isSafeInteger(id) && id! >= 0 ? id! : null;
  const { data, isLoading, isError, isFetching, refetch } = useRequestHistory(actorId);
  const items = data?.items || [];
  return (
    <div className="min-w-0 space-y-8">
      {isAdmin && <PendingApprovalsPanel />}
      <section aria-labelledby="my-requests-heading" className="space-y-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 id="my-requests-heading" className="text-lg font-semibold">My requests</h2>
          {actorId !== null && !isError && <button type="button" disabled={isFetching}
            onClick={() => { if (!isFetching) void refetch({ cancelRefetch: false }); }} className={controlStyle}>
            {isFetching ? 'Refreshing…' : 'Refresh list'}
          </button>}
        </div>
        {isError && <QueryError message={data ? 'Could not refresh your requests. Previous results are still shown.' : 'Could not load your requests.'}
          busy={isFetching} onRetry={() => { if (!isFetching) void refetch({ cancelRefetch: false }); }} />}
        {actorId === null ? <p role={accountError ? 'alert' : 'status'} className="text-muted text-sm py-6">
          {accountLoading ? 'Loading account…' : accountError ? 'Account unavailable. Request history could not be loaded.' : 'Sign in to view your request history.'}
        </p> : isLoading ? <p className="text-muted text-sm py-6" role="status">Loading your requests…</p> : items.length === 0 ? !isError && (
          <div className="text-center rounded-xl border border-border bg-card px-5 py-12">
            <Icon name="requests" size={28} className="mx-auto mb-4 text-muted" />
            <h3 className="text-lg font-semibold mb-1">No requests yet</h3>
            <p className="text-muted text-sm">Requests you submit will appear here.</p>
          </div>
        ) : (
          <ul className="space-y-3">
            {items.map((r) => (
              <li key={r.id} className="min-w-0 rounded-xl border border-border bg-card p-4 sm:p-5">
                <div className="flex min-w-0 flex-col lg:flex-row gap-4 lg:gap-6">
                  <h3 className="min-w-0 flex-1 text-sm font-semibold [overflow-wrap:anywhere]">{r.title}</h3>
                  <dl className="grid min-w-0 flex-[2] grid-cols-2 sm:grid-cols-3 gap-4 text-sm">
                    <div className="min-w-0"><dt className="text-xs text-muted mb-2">Type</dt><dd>{r.media_type === 'tv' ? 'Series' : 'Movie'}</dd></div>
                    <div className="min-w-0"><dt className="text-xs text-muted mb-2">Review</dt><dd><StatusPill status={r.status} /></dd></div>
                    <div className="min-w-0"><dt className="text-xs text-muted mb-2">Title library</dt><dd><LibraryPill status={r.library_status} /></dd></div>
                    <div className="min-w-0"><dt className="text-xs text-muted mb-2">Requested</dt><dd className="text-xs [overflow-wrap:anywhere]">{r.created_at || 'Not recorded'}</dd></div>
                    <div className="min-w-0 col-span-2 sm:col-span-3"><dt className="text-xs text-muted mb-2">Dispatch</dt><dd><RequestDispatchStatus info={r.request_dispatch} /></dd></div>
                  </dl>
                </div>
              </li>
            ))}
          </ul>
        )}
        {actorId !== null && <p className="text-xs text-muted">Review, dispatch and title library information are separate. A title entry does not confirm this request’s scope coverage or playback. Recent requests only; a missing entry does not confirm a failed submission.</p>}
      </section>

      {isAdmin && <FailedRequestsPanel />}
    </div>
  );
}

function QueryError({ message, busy, onRetry }: { message: string; busy: boolean; onRetry: () => void }) {
  return <div role="alert" className="rounded-xl border border-border bg-card p-5 space-y-3">
    <p className="text-sm">{message}</p>
    <button type="button" onClick={onRetry} disabled={busy} className={controlStyle}>
      {busy ? 'Refreshing…' : 'Refresh list'}
    </button>
  </div>;
}

function FailedRequestsPanel() {
  const qc = useQueryClient();
  const { data, isError, isFetching, refetch } = useQuery({ queryKey: ['failed-requests'], queryFn: api.failedRequests, refetchInterval: 10000 });
  const retryMut = useMutation({
    mutationFn: (id: number) => api.retryRequest(id),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['failed-requests'] }),
        qc.invalidateQueries({ queryKey: ['my-requests'] }),
      ]);
    },
  });

  const items = data?.items || [];
  if (items.length === 0 && !isError && !retryMut.isError) return null;

  const refresh = async () => {
    const result = await refetch();
    if (!result.isError) retryMut.reset();
  };

  return (
    <section className="min-w-0 space-y-4">
      <h2 className="text-lg font-bold mb-3 text-red-400">Failed requests</h2>
      <p className="text-muted text-xs mb-3">
        These requests failed to find a stream. The system retries automatically  -  you can also retry manually.
      </p>
      {(isError || retryMut.isError) && <QueryError
        message={retryMut.isError ? 'Could not confirm the retry. Refresh to check its status.' : 'Could not refresh failed requests.'}
        busy={isFetching} onRetry={() => void refresh()} />}
      {items.length > 0 && <div role="region" aria-label="Failed request details" tabIndex={0} className="max-w-full overflow-x-auto rounded-xl border border-border">
      <table className="w-full min-w-[640px] text-sm">
        <thead className="text-xs text-muted uppercase border-b border-border">
          <tr>
            <th className="text-left py-2 px-3">Title</th>
            <th className="text-left py-2 px-3">Type</th>
            <th className="text-left py-2 px-3">Error</th>
            <th className="text-left py-2 px-3">Updated</th>
            <th className="text-right py-2 px-3">Action</th>
          </tr>
        </thead>
        <tbody>
          {items.map((r: any) => (
            <tr key={r.id} className="border-b border-border/50 hover:bg-card">
              <td className="py-2 px-3 font-medium">{r.title}</td>
              <td className="py-2 px-3 text-muted">{r.media_type}</td>
              <td className="py-2 px-3 text-red-400 text-xs max-w-xs truncate" title={r.error || ''}>
                {r.error || ' - '}
              </td>
              <td className="py-2 px-3 text-muted text-xs">{r.updated_at}</td>
              <td className="py-2 px-3 text-right">
                <button
                  type="button"
                  onClick={() => retryMut.mutate(r.id)}
                  disabled={retryMut.isPending || retryMut.isError || isError}
                  aria-label={`Retry ${r.title}`}
                  className={controlStyle}
                >
                  {retryMut.isPending && retryMut.variables === r.id ? 'Retrying…' : 'Retry'}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>}
    </section>
  );
}

function PendingApprovalsPanel() {
  const qc = useQueryClient();
  const { data, isError, isFetching, refetch } = useQuery({
    queryKey: ['pending-requests'],
    queryFn: () => api.userRequests('pending'),
    refetchInterval: 15000,
  });
  const approveMut = useMutation({
    mutationFn: (id: number) => api.approveRequest(id),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['pending-requests'] }),
        qc.invalidateQueries({ queryKey: ['my-requests'] }),
      ]);
    },
  });
  const denyMut = useMutation({
    mutationFn: ({ id, note }: { id: number; note?: string }) => api.denyRequest(id, note),
    onSuccess: async () => {
      await Promise.all([
        qc.invalidateQueries({ queryKey: ['pending-requests'] }),
        qc.invalidateQueries({ queryKey: ['my-requests'] }),
      ]);
    },
  });

  const items = data?.items || [];
  const uncertain = approveMut.isError || denyMut.isError;
  if (items.length === 0 && !isError && !uncertain) return null;
  const refresh = async () => {
    const result = await refetch();
    if (!result.isError) { approveMut.reset(); denyMut.reset(); }
  };

  return (
    <section className="min-w-0 space-y-4">
      <h2 className="text-lg font-bold mb-3 text-amber">Pending approvals</h2>
      <p className="text-muted text-xs mb-3">
        These requests need your approval before they are processed.
      </p>
      {(isError || uncertain) && <QueryError
        message={uncertain ? 'Could not confirm the update. Refresh before trying again.' : 'Could not refresh pending approvals.'}
        busy={isFetching} onRetry={() => void refresh()} />}
      {items.length > 0 && <div role="region" aria-label="Pending approval details" tabIndex={0} className="max-w-full overflow-x-auto rounded-xl border border-border">
      <table className="w-full min-w-[680px] text-sm">
        <thead className="text-xs text-muted uppercase border-b border-border">
          <tr>
            <th className="text-left py-2 px-3">User</th>
            <th className="text-left py-2 px-3">Title</th>
            <th className="text-left py-2 px-3">Type</th>
            <th className="text-left py-2 px-3">IMDb</th>
            <th className="text-left py-2 px-3">Date</th>
            <th className="text-right py-2 px-3">Actions</th>
          </tr>
        </thead>
        <tbody>
          {items.map((r: any) => (
            <tr key={r.id} className="border-b border-border/50 hover:bg-card">
              <td className="py-2 px-3 font-medium">{r.username || `user #${r.user_id}`}</td>
              <td className="py-2 px-3">{r.title}</td>
              <td className="py-2 px-3 text-muted">{r.media_type}</td>
              <td className="py-2 px-3">
                <a href={`https://www.imdb.com/title/${r.imdb_id}/`} target="_blank" rel="noreferrer"
                   className="text-accent hover:underline text-xs">{r.imdb_id}</a>
              </td>
              <td className="py-2 px-3 text-muted text-xs">{r.created_at}</td>
              <td className="py-2 px-3 text-right space-x-2">
                <button
                  type="button"
                  onClick={() => approveMut.mutate(r.id)}
                  disabled={approveMut.isPending || denyMut.isPending || uncertain || isError}
                  aria-label={`Approve ${r.title}`}
                  className={controlStyle}
                >{approveMut.isPending && approveMut.variables === r.id ? 'Approving…' : 'Approve'}</button>
                <button
                  type="button"
                  onClick={() => {
                    const note = prompt('Reason for denial? (optional)');
                    if (note !== null) denyMut.mutate({ id: r.id, note: note || undefined });
                  }}
                  disabled={approveMut.isPending || denyMut.isPending || uncertain || isError}
                  aria-label={`Deny ${r.title}`}
                  className={controlStyle}
                >{denyMut.isPending && denyMut.variables?.id === r.id ? 'Denying…' : 'Deny'}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>}
    </section>
  );
}

function StatusPill({ status }: { status: string }) {
  const label = ['pending', 'approved', 'denied'].includes(status) ? status : 'Unknown approval status';
  const cls = status === 'approved' ? 'bg-blue-500/20 text-blue-400'
    : status === 'denied' || status === 'failed' ? 'bg-red-500/20 text-red-400' : 'bg-amber/20 text-amber';
  return <span className={`px-2 py-0.5 rounded text-xs font-semibold capitalize ${cls}`}>{label}</span>;
}

function LibraryPill({ status }: { status: string | null | undefined }) {
  if (!status) return <span className="text-xs text-muted">Unknown</span>;
  const map: Record<string, { cls: string; label: string }> = {
    success: { cls: 'bg-ok/20 text-ok', label: 'Indexed title' },
    wanted:  { cls: 'bg-amber/20 text-amber', label: 'Wanted' },
    upcoming:{ cls: 'bg-blue-500/20 text-blue-400', label: 'Upcoming' },
    failed:  { cls: 'bg-red-500/20 text-red-400', label: 'Failed' },
    pending: { cls: 'bg-amber/20 text-amber', label: 'Title processing' },
  };
  const m = Object.prototype.hasOwnProperty.call(map, status) ? map[status]
    : { cls: 'bg-gray-500/20 text-gray-400', label: 'Unknown library status' };
  return <span className={`px-2 py-0.5 rounded text-xs font-semibold ${m.cls}`}>{m.label}</span>;
}
