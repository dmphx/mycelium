import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api';

type Tab = 'movies' | 'series';

export default function Library() {
  const [tab, setTab] = useState<Tab>('movies');
  return (
    <div>
      <div className="flex gap-2 border-b border-border mb-5">
        {(['movies', 'series'] as Tab[]).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTab(t)}
            className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px capitalize transition ${
              tab === t ? 'border-accent text-white' : 'border-transparent text-muted hover:text-white'
            }`}
          >
            {t}
          </button>
        ))}
      </div>
      {tab === 'movies' ? <MoviesPanel /> : <SeriesPanel />}
    </div>
  );
}

function MoviesPanel() {
  const { data, isLoading } = useQuery({ queryKey: ['stats'], queryFn: api.stats });
  if (isLoading) return <div className="text-muted">Loading…</div>;
  const items = data?.movies || [];
  return (
    <div>
      <p className="text-muted text-sm mb-4">{items.length} movies in your library</p>
      <table className="w-full text-sm">
        <thead className="text-xs text-muted uppercase border-b border-border">
          <tr>
            <th className="text-left py-2 px-3">Title</th>
            <th className="text-left py-2 px-3">Year</th>
            <th className="text-left py-2 px-3">Quality</th>
            <th className="text-left py-2 px-3">Added</th>
          </tr>
        </thead>
        <tbody>
          {items.map((m: any, i: number) => (
            <tr key={i} className="border-b border-border/50 hover:bg-card">
              <td className="py-2 px-3">{m.title}</td>
              <td className="py-2 px-3 text-muted">{m.year || '—'}</td>
              <td className="py-2 px-3 text-muted">{m.quality || '—'}</td>
              <td className="py-2 px-3 text-muted text-xs">{m.created_at || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function SeriesPanel() {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const { data, isLoading } = useQuery({
    queryKey: ['library-series-episodes'],
    queryFn: () => fetch('/ui/api/library/series-episodes').then(r => r.json()),
  });
  if (isLoading) return <div className="text-muted">Loading…</div>;
  const series: any[] = data?.series || [];

  const toggle = (title: string) => {
    setExpanded(prev => {
      const next = new Set(prev);
      next.has(title) ? next.delete(title) : next.add(title);
      return next;
    });
  };

  return (
    <div>
      <p className="text-muted text-sm mb-4">{series.length} series in library</p>
      <div className="space-y-1">
        {series.map((s: any) => {
          const isOpen = expanded.has(s.title);
          const totalEps = s.seasons.reduce((n: number, se: any) => n + se.episodes.length, 0);
          return (
            <div key={s.title} className="border border-border rounded">
              <button
                type="button"
                onClick={() => toggle(s.title)}
                className="w-full flex items-center justify-between px-4 py-3 text-sm hover:bg-card transition text-left"
              >
                <span className="font-medium">{s.title}</span>
                <span className="text-muted text-xs">
                  {s.seasons.length} season{s.seasons.length !== 1 ? 's' : ''} · {totalEps} episodes
                  <span className="ml-2">{isOpen ? '▲' : '▼'}</span>
                </span>
              </button>
              {isOpen && (
                <div className="border-t border-border px-4 py-3 space-y-2 bg-card/50">
                  {s.seasons.map((se: any) => (
                    <div key={se.season}>
                      <div className="text-xs text-muted mb-1">
                        Season {String(se.season).padStart(2, '0')} — {se.episodes.length} episode{se.episodes.length !== 1 ? 's' : ''}
                      </div>
                      <div className="flex flex-wrap gap-1">
                        {se.episodes.map((ep: number) => (
                          <span key={ep} className="text-xs bg-accent/20 text-accent px-2 py-0.5 rounded">
                            E{String(ep).padStart(2, '0')}
                          </span>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
