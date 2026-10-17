import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';

export default function Requests() {
  const { data: sessionData } = useQuery({ queryKey: ['session'], queryFn: api.session });
  const isAdmin = sessionData?.user?.role === 'admin';
  const { data, isLoading } = useQuery({ queryKey: ['my-requests'], queryFn: api.myRequests });
  if (isLoading) return <div className="text-muted">Loading…</div>;
  const items = data?.items || [];
  return (
    <div className="space-y-8">
      {isAdmin && <PendingApprovalsPanel />}
      <section>
        <h2 className="text-lg font-bold mb-3">My requests</h2>
        {items.length === 0 ? (
          <div className="text-center py-8">
            <div className="text-5xl mb-3">📋</div>
            <h2 className="text-lg font-semibold mb-1">No requests yet</h2>
            <p className="text-muted text-sm">Anything you add from Discover shows up here.</p>
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-xs text-muted uppercase border-b border-border">
              <tr>
                <th className="text-left py-2 px-3">Title</th>
                <th className="text-left py-2 px-3">Type</th>
                <th className="text-left py-2 px-3">Approval</th>
                <th className="text-left py-2 px-3">Library</th>
                <th className="text-left py-2 px-3">Requested</th>
              </tr>
            </thead>
            <tbody>
              {items.map((r: any) => (
                <tr key={r.id} className="border-b border-border/50 hover:bg-card">
                  <td className="py-2 px-3 font-medium">{r.title}</td>
                  <td className="py-2 px-3 text-muted">{r.media_type}</td>
                  <td className="py-2 px-3">
                    <StatusPill status={r.status} />
                  </td>
                  <td className="py-2 px-3">
                    <LibraryPill status={r.library_status} />
                  </td>
                  <td className="py-2 px-3 text-muted text-xs">{r.created_at}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {isAdmin && <FailedRequestsPanel />}
    </div>
  );
}

function FailedRequestsPanel() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ['failed-requests'], queryFn: api.failedRequests, refetchInterval: 10000 });
  const retryMut = useMutation({
    mutationFn: (id: number) => api.retryRequest(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['failed-requests'] });
    },
  });

  const items = data?.items || [];
  if (items.length === 0) return null;

  return (
    <section>
      <h2 className="text-lg font-bold mb-3 text-red-400">Failed requests</h2>
      <p className="text-muted text-xs mb-3">
        These requests failed to find a stream. The system retries automatically  -  you can also retry manually.
      </p>
      <table className="w-full text-sm">
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
                  onClick={() => retryMut.mutate(r.id)}
                  disabled={retryMut.isPending}
                  className="px-3 py-1 rounded bg-accent/20 text-accent text-xs hover:bg-accent/30 disabled:opacity-50"
                >
                  ↺ Retry
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function PendingApprovalsPanel() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ['pending-requests'],
    queryFn: () => api.userRequests('pending'),
    refetchInterval: 15000,
  });
  const approveMut = useMutation({
    mutationFn: (id: number) => api.approveRequest(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pending-requests'] });
      qc.invalidateQueries({ queryKey: ['my-requests'] });
    },
  });
  const denyMut = useMutation({
    mutationFn: ({ id, note }: { id: number; note?: string }) => api.denyRequest(id, note),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['pending-requests'] });
    },
  });

  const items = data?.items || [];
  if (items.length === 0) return null;

  return (
    <section>
      <h2 className="text-lg font-bold mb-3 text-amber">Pending approvals</h2>
      <p className="text-muted text-xs mb-3">
        These requests need your approval before they are processed.
      </p>
      <table className="w-full text-sm">
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
                  onClick={() => approveMut.mutate(r.id)}
                  disabled={approveMut.isPending}
                  className="px-3 py-1 rounded bg-ok/20 text-ok text-xs hover:bg-ok/30 disabled:opacity-50"
                >Approve</button>
                <button
                  onClick={() => {
                    const note = prompt('Reason for denial? (optional)');
                    if (note !== null) denyMut.mutate({ id: r.id, note: note || undefined });
                  }}
                  disabled={denyMut.isPending}
                  className="px-2 py-1 rounded bg-red-500/20 text-red-400 text-xs hover:bg-red-500/30 disabled:opacity-50"
                >Deny</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function StatusPill({ status }: { status: string }) {
  const cls =
    status === 'approved' || status === 'completed' ? 'bg-ok/20 text-ok' :
    status === 'denied' ? 'bg-red-500/20 text-red-400' :
    status === 'failed' ? 'bg-red-500/20 text-red-400' :
    'bg-amber/20 text-amber';
  return <span className={`px-2 py-0.5 rounded text-xs font-semibold capitalize ${cls}`}>{status}</span>;
}

function LibraryPill({ status }: { status: string | null }) {
  if (!status) return <span className="text-xs text-muted">--</span>;
  const map: Record<string, { cls: string; label: string }> = {
    success: { cls: 'bg-ok/20 text-ok', label: 'In library' },
    wanted:  { cls: 'bg-amber/20 text-amber', label: 'Wanted' },
    upcoming:{ cls: 'bg-blue-500/20 text-blue-400', label: 'Upcoming' },
    failed:  { cls: 'bg-red-500/20 text-red-400', label: 'Failed' },
    pending: { cls: 'bg-amber/20 text-amber', label: 'Processing' },
  };
  const m = map[status] || { cls: 'bg-gray-500/20 text-gray-400', label: status };
  return <span className={`px-2 py-0.5 rounded text-xs font-semibold ${m.cls}`}>{m.label}</span>;
}
