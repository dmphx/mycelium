import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import type { MaintenanceJob, MaintenanceJobId, MaintenanceRun } from '../types';

const actions: { id: MaintenanceJobId; title: string; description: string; manual: string }[] = [
  {
    id: 'migrate_canonical', title: 'Migrate to canonical names', manual: 'One-time · manual',
    description: 'Renames movie folders to TMDB canonical names using IMDb identity and merges duplicate folders. Run once when names need correcting, then do a full Jellyfin library rescan.',
  },
  {
    id: 'cleanup_duplicates', title: 'Clean up duplicate .strm files', manual: 'After migration · manual',
    description: 'Removes extra .strm files, keeping the file matching the folder name where possible. Use after migration or when duplicates are present, then rescan Jellyfin.',
  },
  {
    id: 'repair_strms', title: 'Repair broken .strm links', manual: 'Manual',
    description: 'Scans movie links for expired URLs, missing files and orphaned tokens. Re-links them to a proxy token where possible, or deletes and requeues them for processing.',
  },
  {
    id: 'scan_torbox', title: 'Scan TorBox library', manual: 'On demand · manual',
    description: 'Imports cached TorBox content that Mycelium does not know about. Useful after database recovery or for content added outside Mycelium. In Catbox mode, the regular generator only rebuilds known items. Rescan Jellyfin after an import.',
  },
];

const labels: Record<string, string> = {
  scanned: 'Scanned', ok: 'Valid links', renamed: 'Renamed', merged: 'Merged',
  cleaned: 'Files removed', imported: 'Imported', missing_strm: 'Missing files found',
  orphaned_tokens: 'Orphaned tokens found', relinked: 'Re-linked', requeued: 'Requeued',
  skipped: 'Skipped', no_imdb: 'Missing IMDb ID', errors: 'Errors', failed: 'Failed imports',
};

function timestamp(value: string | null) {
  return value ? new Date(value).toLocaleString() : 'None recorded';
}

function scheduleLabel(job: MaintenanceJob | undefined, manual: string) {
  if (!job) return 'Loading schedule…';
  if (job.schedule.mode === 'manual') return manual;
  if (job.schedule.mode === 'disabled') return 'Automatic repair off · manual available';
  if (job.schedule.mode === 'paused') return 'Automatic repair paused';
  const seconds = job.schedule.interval_seconds;
  if (!seconds) return 'Automatic';
  const hours = seconds / 3600;
  return `Automatic · every ${hours >= 1 ? `${hours} hours` : `${seconds / 60} minutes`}`;
}

function RunResult({ run }: { run: MaintenanceRun }) {
  const names: Record<MaintenanceRun['status'], string> = {
    running: 'Running', succeeded: 'Completed', partial: 'Completed with errors',
    failed: 'Failed', deferred: 'Deferred', interrupted: 'Completion unknown',
  };
  const color = run.status === 'succeeded' ? 'text-ok'
    : run.status === 'running' ? 'text-muted' : 'text-warn';
  const counts = Object.keys(labels).filter(key => key in run.result && (key === 'scanned' || run.result[key] > 0));
  return (
    <div className="space-y-1 text-xs" aria-live="polite">
      <p>
        <span className={`font-semibold ${color}`}>{names[run.status]}</span>
        <span className="text-muted"> · {run.source === 'scheduled' ? 'Scheduled' : 'Manual'} · {timestamp(run.finished_at || run.started_at)}
          {run.duration_seconds !== null && ` · ${Math.round(run.duration_seconds)}s`}
        </span>
      </p>
      {run.message && <p className="text-warn">{run.message}</p>}
      {counts.length > 0 && (
        <p className="text-muted">{counts.map(key => `${labels[key]}: ${run.result[key]}`).join(' · ')}</p>
      )}
      {(run.result.requeued ?? 0) > 0 && <p className="text-muted">Requeued items were submitted for processing; completion is tracked separately.</p>}
    </div>
  );
}

export default function MaintenancePanel() {
  const qc = useQueryClient();
  const queryKey = ['maintenance-status'];
  const status = useQuery({ queryKey, queryFn: api.maintenanceStatus, refetchInterval: 10000 });
  const run = useMutation({
    mutationFn: api.runMaintenance,
    onSettled: () => qc.invalidateQueries({ queryKey }),
  });
  const busy = run.isPending || status.data?.jobs.some(job => job.active_run !== null);

  function start(id: MaintenanceJobId) {
    if (id === 'migrate_canonical' && !confirm('This renames movie folders to TMDB canonical names and removes duplicates. Jellyfin needs a full rescan afterwards. Continue?')) return;
    run.mutate(id);
  }

  return (
    <section>
      <h2 className="text-lg font-bold mb-2">Maintenance</h2>
      <p className="text-muted text-sm mb-4">
        Scheduled and manual runs are recorded here. History starts when run tracking is installed;
        older runs have no saved result. Completion describes the maintenance pass, not a playback test.
      </p>
      {status.isError && <p role="alert" className="text-warn text-sm mb-3">Maintenance status could not refresh. Displayed records may be out of date.</p>}
      <div className="bg-card rounded-lg border border-border divide-y divide-border">
        {actions.map(action => {
          const job = status.data?.jobs.find(item => item.id === action.id);
          const active = (run.isPending && run.variables === action.id) || !!job?.active_run;
          const latest = job?.last_run;
          return (
            <div key={action.id} className="p-4 space-y-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h3 className="text-sm font-semibold">{action.title}</h3>
                <span className="rounded border border-border px-2 py-1 text-xs text-muted">
                  {status.isError && !job ? 'Schedule unavailable' : scheduleLabel(job, action.manual)}
                </span>
              </div>
              <p className="text-muted text-xs leading-relaxed">{action.description}</p>
              {job && (
                <div className="space-y-2">
                  <p className="text-xs text-muted">Last successful run: {timestamp(job.last_success_at)}</p>
                  {job.schedule.mode === 'automatic' && job.schedule.next_run_at && (
                    <p className="text-xs text-muted">Next scheduled run: {timestamp(job.schedule.next_run_at)}</p>
                  )}
                  {job.active_run && job.active_run.id !== latest?.id && <RunResult run={job.active_run} />}
                  {latest ? <RunResult run={latest} /> : <p className="text-xs text-muted">No recorded run.</p>}
                </div>
              )}
              {run.isError && run.variables === action.id && (
                <p role="alert" className="text-xs text-warn">{run.error.message}</p>
              )}
              <button onClick={() => start(action.id)} disabled={busy}
                className="px-3 py-1.5 rounded bg-accent text-sm font-semibold disabled:opacity-50">
                {active ? 'Running…' : action.id === 'repair_strms' ? 'Run repair now' : action.title}
              </button>
            </div>
          );
        })}
      </div>
    </section>
  );
}
