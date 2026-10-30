import RequestDispatchStatus from './RequestDispatchStatus';
import { readWatchlist } from '../hooks/readWatchlist';
import { useEffect, useState } from 'react';
import { createPortal } from 'react-dom';
import { Link } from 'react-router-dom';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { api, tmdbImg } from '../api';
import type { MediaType, TmdbItem, WatchlistItem } from '../types';
import TrailerModal from './TrailerModal';
import PersonModal from './PersonModal';
import { usePluginSlot } from '../hooks/usePluginSlots';
import { useWatched } from '../hooks/useWatched';
import { useLibraryRequest } from '../hooks/useLibraryRequest';
import { useModalDialog } from '../hooks/useModalDialog';

type DetailModalProps = {
  tmdbId: number | null;
  mediaType: MediaType | null;
  onClose: () => void;
  onSelectItem: (item: TmdbItem) => void;
};

export default function DetailModal(props: DetailModalProps) {
  // Each title owns its scope, polling and nested dialogs. Changing titles must
  // never carry an in-flight request or selected seasons into the next title.
  return <DetailContent key={`${props.mediaType}:${props.tmdbId}`} {...props} />;
}

function DetailContent({ tmdbId, mediaType, onClose, onSelectItem }: DetailModalProps) {
  const queryClient = useQueryClient();
  const open = tmdbId !== null && mediaType !== null;

  const { data: detail, isLoading, isError, refetch } = useQuery({
    queryKey: ['detail', mediaType, tmdbId],
    queryFn: () => api.details(mediaType!, tmdbId!),
    enabled: open,
  });

  const { data: watchlist } = useQuery({
    queryKey: ['watchlist'],
    queryFn: ({ signal }) => readWatchlist(signal), retry: false, enabled: open,
  });

  const inWatchlist =
    detail?.imdb_id &&
    watchlist?.items.some(
      (w: WatchlistItem) => w.imdb_id === detail.imdb_id && w.media_type === detail.media_type,
    );

  const libStatus = detail?.library_status as string | undefined;
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session });
  // webplayer_enabled is injected by the webplayer plugin; absent when plugin not loaded
  const canPlay = !!(session?.user as any)?.webplayer_enabled;
  const PlayerModal = usePluginSlot('episode-player');
  const watched = useWatched();
  const isWatched = !!(detail?.imdb_id && watched.has(detail.imdb_id));

  const requestFlow = useLibraryRequest(session?.authenticated ? String(session.user?.id ?? 'legacy') : null, mediaType, tmdbId);
  const addStatus = requestFlow.attempt.phase;
  const [personId, setPersonId] = useState<number | null>(null);

  // TV monitoring scope
  const [showTrailer, setShowTrailer] = useState(false);
  const [showPlayer, setShowPlayer] = useState(false);
  const dialogRef = useModalDialog({ open, layer: 200, onClose, suspended: showPlayer });
  const [draftMonitorMode, setMonitorMode] = useState<'all' | 'future' | 'selected'>('all');
  const [draftSelectedSeasons, setSelectedSeasons] = useState<number[]>([]);

  const submittedSelection = requestFlow.attempt.selection;
  const monitorMode = submittedSelection?.mode ?? draftMonitorMode;
  const selectedSeasons = submittedSelection?.seasons ?? draftSelectedSeasons;

  const toggleSeason = (n: number) =>
    setSelectedSeasons((prev) =>
      prev.includes(n) ? prev.filter((x) => x !== n) : [...prev, n].sort((a, b) => a - b),
    );

  const watchlistMutation = useMutation({
    mutationFn: async () => {
      if (!detail?.imdb_id) throw new Error('no imdb id');
      if (inWatchlist) {
        return api.watchlistRemove(detail.imdb_id, detail.media_type);
      }
      return api.watchlistAdd({
        imdb_id: detail.imdb_id,
        tmdb_id: detail.tmdb_id,
        media_type: detail.media_type,
        title: detail.title,
        poster_path: detail.poster_path,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['watchlist'] });
    },
  });

  // Reset state when modal opens fresh
  useEffect(() => {
    if (open) { setShowTrailer(false); setShowPlayer(false); }
  }, [open, tmdbId]);

  if (!open) return null;

  const poster = tmdbImg.poster(detail?.poster_path);
  const backdrop = tmdbImg.backdrop(detail?.backdrop_path);
  const trailer = detail?.trailers?.[0];

  return createPortal(
    <>
    <div
      className="fixed inset-0 z-[200] bg-black/85 backdrop-blur-sm overflow-y-auto p-4 sm:p-8"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div ref={dialogRef} role="dialog" tabIndex={-1}
        aria-modal={personId === null && !showTrailer && !showPlayer ? true : undefined}
        aria-label={detail ? `${detail.title}${detail.year ? ` (${detail.year})` : ''}` : isError ? 'Title details unavailable' : 'Title details'}
        className="relative max-w-5xl mx-auto bg-card rounded-2xl overflow-hidden shadow-2xl">
        {/* Backdrop hero */}
        {backdrop && (
          <div
            className="h-64 sm:h-80 bg-cover bg-center relative"
            style={{ backgroundImage: `url(${backdrop})` }}
          >
            <div className="absolute inset-0 bg-gradient-to-t from-card via-card/60 to-transparent" />
          </div>
        )}
        <button
          type="button"
          onClick={onClose}
          className="absolute top-3 right-3 z-10 w-9 h-9 rounded-full bg-black/60 hover:bg-black/80
                      text-white text-xl flex items-center justify-center"
          aria-label="Close"
        >
          ×
        </button>

        <div className={`p-6 sm:p-8 ${backdrop ? '-mt-32 relative' : ''}`}>
          {isError ? (
            <div className="py-12 text-center space-y-3" role="alert">
              <p className="text-muted">Title details could not be loaded.</p>
              <button type="button" onClick={() => void refetch()}
                className="min-h-11 px-4 rounded-lg border border-border hover:bg-white/5">
                Retry details
              </button>
            </div>
          ) : isLoading || !detail ? (
            <div className="text-muted text-center py-12" role="status">Loading title details...</div>
          ) : (
            <div className="flex flex-col sm:flex-row gap-6">
              <div className="flex-shrink-0 w-40 sm:w-52 mx-auto sm:mx-0 aspect-[2/3] rounded-lg overflow-hidden bg-bg">
                {poster ? (
                  <img src={poster} alt={detail.title} className="w-full h-full object-cover" />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-muted text-xs p-3">
                    No poster
                  </div>
                )}
              </div>
              <div className="flex-1 min-w-0">
                <h2 className="text-2xl sm:text-3xl font-bold">
                  {detail.title}{' '}
                  {detail.year && (
                    <span className="text-muted font-normal">({detail.year})</span>
                  )}
                </h2>
                {detail.tagline && (
                  <p className="text-muted italic mt-1">{detail.tagline}</p>
                )}
                <div className="flex flex-wrap gap-2 mt-3 text-xs">
                  {isWatched && (
                    <span className="px-2 py-0.5 rounded bg-green-600 text-white font-semibold">✓ Watched</span>
                  )}
                  {detail.rating > 0 && (
                    <Badge>★ {detail.rating} ({detail.votes} votes)</Badge>
                  )}
                  {detail.runtime ? <Badge>{detail.runtime} min</Badge> : null}
                  {detail.genres?.map((g) => (
                    <Badge key={g}>{g}</Badge>
                  ))}
                  {detail.status && <Badge>{detail.status}</Badge>}
                  {detail.media_type === 'tv' && detail.number_of_seasons && (
                    <Badge>
                      {detail.number_of_seasons} seasons / {detail.number_of_episodes} eps
                    </Badge>
                  )}
                </div>
                <p className="text-sm leading-relaxed mt-4 max-w-3xl">
                  {detail.overview || 'No overview available.'}
                </p>

                {detail.media_type === 'tv' && (
                  <div className="mt-4 bg-bg/60 border border-border rounded-lg p-3">
                    <div className="text-[10px] uppercase tracking-wider text-muted font-semibold mb-2">
                      What to monitor
                    </div>
                    <div className="flex flex-wrap gap-2 mb-2">
                      {([
                        ['all', 'All seasons'],
                        ['future', 'Future episodes only'],
                        ['selected', 'Pick seasons'],
                      ] as const).map(([mode, label]) => (
                        <button
                          key={mode}
                          type="button"
                          onClick={() => setMonitorMode(mode)}
                          aria-pressed={monitorMode === mode}
                          disabled={addStatus !== 'idle'}
                          className={`text-xs px-3 py-1.5 rounded border ${
                            monitorMode === mode
                              ? 'border-accent bg-accent/10 text-white'
                              : 'border-border text-muted hover:text-white'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    {monitorMode === 'selected' && detail.seasons && (
                      <div className="flex flex-wrap gap-1.5 mt-2">
                        {detail.seasons
                          .filter((s) => s.season_number >= 1)
                          .map((s) => (
                            <button
                              key={s.season_number}
                              type="button"
                              onClick={() => toggleSeason(s.season_number)}
                              aria-pressed={selectedSeasons.includes(s.season_number)}
                              disabled={addStatus !== 'idle'}
                              className={`text-xs px-2 py-1 rounded border ${
                                selectedSeasons.includes(s.season_number)
                                  ? 'border-accent bg-accent text-white'
                                  : 'border-border text-muted hover:text-white'
                              }`}
                              title={`${s.episode_count} eps`}
                            >
                              S{s.season_number}
                            </button>
                          ))}
                      </div>
                    )}
                    {submittedSelection && (
                      <p className="text-xs text-muted mt-2">
                        Submitted selection: {submittedSelection.mode === 'selected'
                          ? `seasons ${submittedSelection.seasons.join(', ')}`
                          : submittedSelection.mode === 'future' ? 'future episodes only' : 'all seasons'}.
                        {' '}This records your submission, not confirmed library coverage.
                      </p>
                    )}
                    {monitorMode === 'future' && (
                      <p className="text-[11px] text-muted mt-1">
                        Only episodes airing from now on  -  the back-catalog is skipped.
                      </p>
                    )}
                  </div>
                )}

                <div className="flex flex-wrap gap-2 mt-5">
                  <LibraryButton
                    libStatus={libStatus}
                    addStatus={addStatus}
                    mediaType={detail.media_type}
                    disabled={
                      !requestFlow.canSubmit || (detail.media_type === 'tv' &&
                      monitorMode === 'selected' &&
                      selectedSeasons.length === 0)
                    }
                    onAdd={() => void requestFlow.submit({
                      tmdb_id: detail.tmdb_id, media_type: detail.media_type, title: detail.title,
                      ...(detail.media_type === 'tv' ? { monitor_mode: monitorMode, seasons: [...selectedSeasons] } : {}),
                    }, detail.imdb_id)}
                  />
                  {canPlay && (libStatus === 'available' || libStatus === 'success') && (
                    <button
                      type="button"
                      onClick={() => setShowPlayer(true)}
                      className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500
                                 text-white font-semibold text-sm transition-colors"
                    >
                      ▶ Play
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => watchlistMutation.mutate()}
                    disabled={!detail.imdb_id || watchlistMutation.isPending}
                    className="px-4 py-2 rounded-lg border border-border hover:bg-bg text-sm
                                disabled:opacity-50"
                  >
                    {inWatchlist ? '★ In watchlist' : '☆ Watchlist'}
                  </button>
                  {trailer && (
                    <button
                      type="button"
                      onClick={() => setShowTrailer(true)}
                      className="px-4 py-2 rounded-lg border border-border hover:bg-bg text-sm"
                    >
                      ▶ Trailer
                    </button>
                  )}
                  {detail.imdb_id && (
                    <a
                      href={`https://www.imdb.com/title/${detail.imdb_id}/`}
                      target="_blank"
                      rel="noopener"
                      className="px-4 py-2 rounded-lg border border-border hover:bg-bg text-sm"
                    >
                      IMDB
                    </a>
                  )}
                </div>

                {(addStatus !== 'idle' || requestFlow.unavailable) && (
                  <div className="mt-4 space-y-3 text-sm" role={['unknown', 'status_error', 'failed', 'approval_error', 'denied'].includes(addStatus) ? 'alert' : 'status'}>
                    <p>{requestFlow.unavailable ? 'Request controls are unavailable. Open request history for the current state.'
                      : addStatus === 'unknown' ? 'Could not confirm the request outcome. Check request history before submitting again.'
                      : addStatus === 'status_error' ? "Your request was accepted, but the title's library status could not be confirmed."
                      : addStatus === 'failed' ? 'A processing failure was reported for this title. Check request history for details.'
                      : addStatus === 'checking_approval' ? 'Checking your request review and dispatch status…'
                      : addStatus === 'approval_error' ? 'Could not refresh your request status. Any previous handoff state may be out of date. Check request history.'
                      : addStatus === 'denied' ? 'Your request was denied. View request history for details.'
                      : addStatus === 'pending' ? 'Request recorded and awaiting approval.'
                      : addStatus === 'approved' ? 'Request approved. Dispatch status is shown separately below.'
                      : addStatus === 'processing' ? 'Legacy request queued. Checking title-level library status…'
                      : addStatus === 'registered' ? 'Library entry found.'
                      : addStatus === 'wanted' ? 'This title is waiting for a release. Check request history for your request.'
                      : addStatus === 'upcoming' ? 'Request accepted. This title is upcoming.'
                      : 'Submitting request…'}</p>
                    {requestFlow.attempt.dispatch && ['approved', 'approval_error', 'checking_approval'].includes(addStatus) && (
                      <RequestDispatchStatus info={requestFlow.attempt.dispatch} />
                    )}
                    <div className="flex flex-wrap gap-3">
                      {['pending', 'approved', 'approval_error', 'checking_approval'].includes(addStatus) && requestFlow.attempt.requestId && (
                        <button type="button" onClick={() => void requestFlow.refreshApproval()} disabled={addStatus === 'checking_approval'}
                          className="min-h-11 px-4 rounded-lg border border-border hover:bg-raised disabled:opacity-50">
                          {addStatus === 'checking_approval' ? 'Checking request status…' : 'Refresh request status'}
                        </button>
                      )}
                      {['status_error', 'failed', 'wanted', 'upcoming'].includes(addStatus) && requestFlow.attempt.imdb && (
                        <button type="button" onClick={requestFlow.refreshStatus} className="min-h-11 px-4 rounded-lg border border-border hover:bg-raised">
                          Refresh request status
                        </button>
                      )}
                      <Link to="/requests" onClick={onClose} className="min-h-11 px-4 py-3 rounded-lg border border-border hover:bg-raised">View request history</Link>
                    </div>
                  </div>
                )}

                {detail.providers?.flatrate && detail.providers.flatrate.length > 0 && (
                  <div className="mt-5">
                    <div className="text-[10px] uppercase tracking-wider text-muted font-semibold mb-2">
                      Streaming on
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {detail.providers.flatrate.map((p) => (
                        <img
                          key={p.id}
                          src={tmdbImg.logo(p.logo_path) || undefined}
                          alt={p.name}
                          title={p.name}
                          className="w-10 h-10 rounded-md"
                        />
                      ))}
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {detail?.media_type === 'tv' && detail.seasons && detail.seasons.length > 0 && (
            <Section title="Seasons">
              <div className="flex gap-3 overflow-x-auto scrollbar-hidden">
                {detail.seasons.map((s) => (
                  <div key={s.season_number} className="flex-shrink-0 w-24 text-center">
                    <div className="aspect-[2/3] rounded-md bg-bg overflow-hidden">
                      {s.poster_path && (
                        <img
                          src={tmdbImg.logo(s.poster_path) || undefined}
                          className="w-full h-full object-cover"
                          alt={s.name}
                        />
                      )}
                    </div>
                    <div className="text-xs mt-1 font-semibold">S{s.season_number}</div>
                    <div className="text-[10px] text-muted">{s.episode_count} eps</div>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {detail?.cast && detail.cast.length > 0 && (
            <Section title="Cast">
              <div className="flex gap-3 overflow-x-auto scrollbar-hidden">
                {detail.cast.map((c, i) => (
                  <button
                    key={i}
                    type="button"
                    onClick={() => c.id && setPersonId(c.id)}
                    disabled={!c.id}
                    className="flex-shrink-0 w-20 text-center group"
                  >
                    <div className="w-20 h-20 rounded-full bg-bg overflow-hidden group-hover:ring-2 group-hover:ring-accent/60 transition">
                      {c.profile_path && (
                        <img
                          src={tmdbImg.profile(c.profile_path) || undefined}
                          alt={c.name}
                          className="w-full h-full object-cover"
                        />
                      )}
                    </div>
                    <div className="text-[11px] mt-1 font-semibold leading-tight line-clamp-2 group-hover:text-accent transition">
                      {c.name}
                    </div>
                    <div className="text-[10px] text-muted line-clamp-2">{c.character}</div>
                  </button>
                ))}
              </div>
            </Section>
          )}

          {detail?.recommendations && detail.recommendations.length > 0 && (
            <Section title="You might also like">
              <div className="grid grid-cols-3 sm:grid-cols-4 md:grid-cols-6 gap-3">
                {detail.recommendations.slice(0, 12).map((r) => (
                  <button
                    key={`${r.media_type}-${r.tmdb_id}`}
                    type="button"
                    onClick={() => onSelectItem(r)}
                    className="aspect-[2/3] rounded-md overflow-hidden bg-bg border border-border
                                hover:border-accent/50 transition"
                  >
                    {r.poster_path ? (
                      <img
                        src={tmdbImg.poster(r.poster_path) || undefined}
                        alt={r.title}
                        className="w-full h-full object-cover"
                      />
                    ) : (
                      <div className="text-xs text-muted p-2 text-center">{r.title}</div>
                    )}
                  </button>
                ))}
              </div>
            </Section>
          )}
        </div>
      </div>
    </div>
    {showTrailer && (
      <TrailerModal
        youtubeKey={trailer ? trailer.key : null}
        title={detail?.title || ''}
        onClose={() => setShowTrailer(false)}
      />
    )}
    {showPlayer && detail?.imdb_id && PlayerModal && (
      <PlayerModal
        imdb_id={detail.imdb_id}
        media_type={detail.media_type}
        title={detail.title}
        onClose={() => setShowPlayer(false)}
      />
    )}
    <PersonModal
      personId={personId}
      onClose={() => setPersonId(null)}
      onSelectItem={(item) => { setPersonId(null); onSelectItem(item); }}
    />
  </>,
  document.body
  );
}

function LibraryButton({
  libStatus,
  addStatus,
  mediaType,
  disabled,
  onAdd,
}: {
  libStatus: string | undefined;
  addStatus: string;
  mediaType: string;
  disabled: boolean;
  onAdd: () => void;
}) {
  if (addStatus === 'idle' && (libStatus === 'available' || libStatus === 'success')) {
    return (
      <button type="button" disabled className="px-4 py-2 rounded-lg bg-green-600 text-white font-semibold text-sm cursor-default">
        In library
      </button>
    );
  }
  if (addStatus === 'idle' && ['wanted', 'upcoming', 'pending', 'failed'].includes(libStatus || '')) {
    const labels: Record<string, string> = { wanted: 'Wanted', upcoming: 'Upcoming', pending: 'Processing', failed: 'Previous processing failed' };
    return <button type="button" disabled className="px-4 py-2 rounded-lg bg-yellow-600 text-white font-semibold text-sm cursor-default">{labels[libStatus!]}</button>;
  }
  const labels: Record<string, string> = {
    approved: 'Approved', checking_approval: 'Checking request status…', approval_error: 'Request status unavailable', denied: 'Request denied',
    submitting: 'Submitting…', processing: 'Processing…', registered: 'Library entry found',
    pending: 'Pending approval', wanted: 'Wanted', upcoming: 'Upcoming', failed: 'Library processing failed',
    unknown: 'Outcome unknown', status_error: 'Status unavailable',
  };
  return (
    <button type="button" onClick={onAdd} disabled={addStatus !== 'idle' || disabled}
      className="min-h-11 px-4 py-2 rounded-lg bg-accent hover:bg-accent/90 disabled:opacity-60 disabled:cursor-not-allowed font-semibold text-sm">
      {labels[addStatus] || (mediaType === 'tv' ? '+ Monitor series' : '+ Add to library')}
    </button>
  );
}

function Badge({ children }: { children: React.ReactNode }) {
  return <span className="bg-bg px-2 py-0.5 rounded text-xs">{children}</span>;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mt-7">
      <h3 className="text-[10px] uppercase tracking-wider text-muted font-semibold mb-3">
        {title}
      </h3>
      {children}
    </div>
  );
}
