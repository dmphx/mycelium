import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../api';
import type { WantedMovie, WantedEpisode } from '../types';

export default function Wanted() {
  const queryClient = useQueryClient();
  const [tab, setTab] = useState<'movies' | 'episodes' | 'playback'>('movies');

  const { data: moviesData, isLoading: moviesLoading } = useQuery({
    queryKey: ['wanted-movies'],
    queryFn: api.wantedMovies,
    refetchInterval: 30_000,
  });

  const { data: episodesData, isLoading: epsLoading } = useQuery({
    queryKey: ['wanted-episodes'],
    queryFn: api.wantedEpisodes,
    refetchInterval: 30_000,
  });
  const { data: playbackData, isLoading: playbackLoading } = useQuery({
    queryKey: ['playability-state'],
    queryFn: api.playabilityState,
    refetchInterval: 30_000,
  });

  const recheckMutation = useMutation({
    mutationFn: api.wantedRecheck,
    onSuccess: () => {
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['wanted-movies'] });
        queryClient.invalidateQueries({ queryKey: ['wanted-episodes'] });
      }, 3000);
    },
  });

  const movies = moviesData?.items ?? [];
  const episodes = episodesData?.items ?? [];
  const playbackIssues = playbackData?.items ?? [];

  const wantedEps = episodes.filter((e) => e.status === 'wanted');
  const notAiredEps = episodes.filter((e) => e.status === 'not_aired');
  const foundEps = episodes.filter((e) => e.status === 'found');

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div className="flex gap-1 bg-card rounded-lg p-1">
          <TabBtn active={tab === 'movies'} onClick={() => setTab('movies')}>
            Movies {movies.length > 0 && <Pill>{movies.length}</Pill>}
          </TabBtn>
          <TabBtn active={tab === 'episodes'} onClick={() => setTab('episodes')}>
            Episodes {wantedEps.length > 0 && <Pill>{wantedEps.length}</Pill>}
          </TabBtn>
          <TabBtn active={tab === 'playback'} onClick={() => setTab('playback')}>
            Playback {playbackIssues.length > 0 && <Pill>{playbackIssues.length}</Pill>}
          </TabBtn>
        </div>
        <button
          type="button"
          onClick={() => recheckMutation.mutate()}
          disabled={recheckMutation.isPending || recheckMutation.isSuccess}
          className="px-4 py-2 rounded-lg bg-accent hover:bg-accent/80 disabled:opacity-60
                     disabled:cursor-not-allowed text-sm font-semibold"
        >
          {recheckMutation.isPending
            ? 'Starting…'
            : recheckMutation.isSuccess
            ? '✓ Recheck running'
            : '↺ Recheck now'}
        </button>
      </div>

      {tab === 'movies' && (
        <section>
          {moviesLoading ? (
            <Spinner />
          ) : movies.length === 0 ? (
            <Empty>No movies on the wanted list.</Empty>
          ) : (
            <div className="rounded-xl border border-border overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-card text-muted text-xs uppercase tracking-wider">
                    <Th>Title</Th>
                    <Th>Reason</Th>
                    <Th>Attempts</Th>
                    <Th>Added</Th>
                    <Th>Last checked</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {movies.map((m) => (
                    <MovieRow key={m.imdb_id} movie={m} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {tab === 'episodes' && (
        <section className="space-y-6">
          {epsLoading ? (
            <Spinner />
          ) : (
            <>
              <EpisodesTable
                title="Searching"
                badge={wantedEps.length}
                rows={wantedEps}
                emptyMsg="No episodes being searched."
              />
              <EpisodesTable
                title="Not yet aired"
                badge={notAiredEps.length}
                rows={notAiredEps}
                emptyMsg="No upcoming episodes tracked."
                dimmed
              />
              <EpisodesTable
                title="Found"
                badge={foundEps.length}
                rows={foundEps}
                emptyMsg=""
                dimmed
                collapsed
              />
            </>
          )}
        </section>
      )}

      {tab === 'playback' && (
        <PlaybackIssues rows={playbackIssues} loading={playbackLoading} />
      )}
    </div>
  );
}

function PlaybackIssues({ rows, loading }: { rows: any[]; loading: boolean }) {
  const queryClient = useQueryClient();
  const [running, setRunning] = useState<string | null>(null);
  const rerun = useMutation({
    mutationFn: (token: string) => api.reResolve(token),
    onMutate: (token) => setRunning(token),
    onSettled: () => {
      setRunning(null);
      setTimeout(() => queryClient.invalidateQueries({ queryKey: ['playability-state'] }), 1000);
    },
  });
  if (loading) return <Spinner />;
  if (!rows.length) return <Empty>No repeated playback failures.</Empty>;
  return (
    <div className="rounded-xl border border-border overflow-hidden">
      <table className="w-full text-sm">
        <thead><tr className="bg-card text-muted text-xs uppercase tracking-wider">
          <Th>Title</Th><Th>Reason</Th><Th>Failures</Th><Th>Last update</Th><Th>Action</Th>
        </tr></thead>
        <tbody className="divide-y divide-border">
          {rows.map((row) => (
            <tr key={`${row.content_key}:${row.token}`} className="hover:bg-card/50 transition">
              <td className="px-4 py-3">
                <div className="font-medium">{row.title || row.content_key}</div>
                <div className="text-[10px] text-muted font-mono">{row.content_key}</div>
              </td>
              <td className="px-4 py-3 text-xs text-red-400">{row.last_fail_reason || 'unknown'}</td>
              <td className="px-4 py-3 text-xs">{row.consecutive_failures}</td>
              <td className="px-4 py-3 text-xs text-muted">{fmtDate(row.updated_at)}</td>
              <td className="px-4 py-3">
                <button type="button" disabled={!row.token || running === row.token}
                  onClick={() => rerun.mutate(row.token)}
                  className="text-xs px-2.5 py-1.5 rounded bg-accent/20 text-accent hover:bg-accent/30 disabled:opacity-50">
                  {running === row.token ? 'Resolving…' : 'Try alternate'}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function MovieRow({ movie }: { movie: WantedMovie }) {
  return (
    <tr className="hover:bg-card/50 transition">
      <td className="px-4 py-3 font-medium">
        <div>{movie.title}</div>
        <div className="text-[10px] text-muted font-mono">{movie.imdb_id}</div>
      </td>
      <td className="px-4 py-3 text-muted text-xs">{movie.reason || ' - '}</td>
      <td className="px-4 py-3 text-center">
        <span className="text-xs px-2 py-0.5 rounded bg-bg">{movie.attempts}</span>
      </td>
      <td className="px-4 py-3 text-xs text-muted">{fmtDate(movie.added_at)}</td>
      <td className="px-4 py-3 text-xs text-muted">{movie.last_checked ? fmtDate(movie.last_checked) : ' - '}</td>
    </tr>
  );
}

function EpisodesTable({
  title,
  badge,
  rows,
  emptyMsg,
  dimmed = false,
  collapsed = false,
}: {
  title: string;
  badge: number;
  rows: WantedEpisode[];
  emptyMsg: string;
  dimmed?: boolean;
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(!collapsed);

  if (rows.length === 0 && !emptyMsg) return null;

  return (
    <div className={dimmed ? 'opacity-60' : ''}>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 mb-2 text-left w-full group"
      >
        <span className="text-xs uppercase tracking-wider text-muted font-semibold group-hover:text-white transition">
          {title}
        </span>
        {badge > 0 && <Pill>{badge}</Pill>}
        <span className="text-muted text-xs ml-auto">{open ? '▲' : '▼'}</span>
      </button>
      {open && (
        <>
          {rows.length === 0 ? (
            <p className="text-sm text-muted">{emptyMsg}</p>
          ) : (
            <div className="rounded-xl border border-border overflow-hidden">
              <table className="w-full text-sm">
                <thead>
                  <tr className="bg-card text-muted text-xs uppercase tracking-wider">
                    <Th>Series</Th>
                    <Th>Episode</Th>
                    <Th>Air date</Th>
                    <Th>Search</Th>
                    <Th>Next retry</Th>
                    <Th>Action</Th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {rows.map((ep) => <EpisodeRow key={ep.id} episode={ep} />)}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function EpisodeRow({ episode: ep }: { episode: WantedEpisode }) {
  const queryClient = useQueryClient();
  const [showTrace, setShowTrace] = useState(false);
  const contentKey = `${ep.imdb_id}:S${String(ep.season).padStart(2, '0')}E${String(ep.episode).padStart(2, '0')}`;
  const trace = useQuery({
    queryKey: ['search-trace', contentKey],
    queryFn: () => api.searchTrace(contentKey),
    enabled: showTrace,
  });
  const search = useMutation({
    mutationFn: () => api.wantedEpisodeSearch(ep.id),
    onSuccess: () => {
      setTimeout(() => {
        queryClient.invalidateQueries({ queryKey: ['wanted-episodes'] });
        queryClient.invalidateQueries({ queryKey: ['search-trace', contentKey] });
      }, 1500);
    },
  });
  const counts = ep.last_search_counts || {};
  const sourceCount = Object.values(ep.last_search_sources || {}).reduce((a, b) => a + b, 0);

  return (
    <>
      <tr className="hover:bg-card/50 transition">
        <td className="px-4 py-3 font-medium">
          <div>{ep.title}</div>
          <div className="text-[10px] text-muted font-mono">{ep.imdb_id}</div>
        </td>
        <td className="px-4 py-3 font-mono text-xs">
          S{String(ep.season).padStart(2, '0')}E{String(ep.episode).padStart(2, '0')}
        </td>
        <td className="px-4 py-3 text-xs text-muted">{ep.air_date || ' - '}</td>
        <td className="px-4 py-3 text-xs">
          <button type="button" onClick={() => setShowTrace((value) => !value)}
            className="text-left hover:text-accent transition">
            <div>{ep.chosen_source || ep.last_search_status || 'Not searched'}</div>
            <div className="text-[10px] text-muted">
              {sourceCount} raw, {counts.ranked || 0} ranked, {ep.attempt_count} attempts
            </div>
          </button>
          {ep.last_search_error && <div className="text-[10px] text-red-400 max-w-xs truncate">{ep.last_search_error}</div>}
        </td>
        <td className="px-4 py-3 text-xs text-muted">
          {ep.next_retry_at ? fmtDate(ep.next_retry_at) : ' - '}
        </td>
        <td className="px-4 py-3">
          <button type="button" onClick={() => search.mutate()}
            disabled={search.isPending}
            className="text-xs px-2.5 py-1.5 rounded bg-accent/20 text-accent hover:bg-accent/30 disabled:opacity-50">
            {search.isPending ? 'Starting…' : 'Search now'}
          </button>
        </td>
      </tr>
      {showTrace && (
        <tr className="bg-card/30">
          <td colSpan={6} className="px-4 py-3">
            {trace.isLoading ? (
              <div className="text-xs text-muted">Loading trace…</div>
            ) : trace.data?.candidates.length ? (
              <div className="space-y-1">
                {trace.data.candidates.slice(0, 8).map((candidate) => (
                  <div key={`${candidate.protocol}:${candidate.info_hash}`}
                    className="grid grid-cols-[2rem_7rem_5rem_1fr] gap-2 text-[11px] font-mono">
                    <span>#{candidate.rank_order || '?'}</span>
                    <span className={candidate.state === 'rejected' ? 'text-red-400' : 'text-muted'}>
                      {candidate.state}
                    </span>
                    <span>{candidate.quality || '?'}</span>
                    <span className="truncate">
                      {candidate.source || '?'} · {candidate.cached_provider || 'not cached'}
                      {candidate.reject_reason ? ` · ${candidate.reject_reason}` : ''}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <div className="text-xs text-muted">No candidate trace recorded yet.</div>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

function TabBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`px-4 py-1.5 rounded text-sm font-medium flex items-center gap-1.5 transition
        ${active ? 'bg-accent text-white' : 'text-muted hover:text-white'}`}
    >
      {children}
    </button>
  );
}

function Pill({ children }: { children: React.ReactNode }) {
  return (
    <span className="bg-accent/20 text-accent text-[10px] font-bold px-1.5 py-0.5 rounded-full">
      {children}
    </span>
  );
}

function Th({ children }: { children: React.ReactNode }) {
  return <th className="px-4 py-2 text-left font-medium">{children}</th>;
}

function Spinner() {
  return <div className="text-muted text-sm py-8 text-center">Loading…</div>;
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-muted text-sm py-12 text-center bg-card/30 rounded-xl border border-border">
      {children}
    </div>
  );
}

function fmtDate(iso: string) {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}
