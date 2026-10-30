import { useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { jsonRequest } from './requests'

interface Sub { language: string; label: string; url: string }

export default function SubtitlePicker({ token, generation, onSelect }: {
  token: string; generation?: string; onSelect: (url: string | null) => void
}) {
  const started = useRef(Date.now())
  const query = useQuery<{ subtitles: Sub[] }>({
    queryKey: ['subtitles', token, generation], retry: false,
    queryFn: async ({ signal }) => {
      const data = await jsonRequest(`/stream/${token}/subtitles`, signal)
      if (!Array.isArray(data.subtitles) || data.subtitles.length > 128) throw new Error('Invalid subtitle response.')
      const prefix = `/stream/${token}/subtitles/${generation}/`
      if (data.subtitles.some((s: Sub) => !s || typeof s.language !== 'string' || typeof s.label !== 'string'
        || typeof s.url !== 'string' || !s.url.startsWith(prefix) || !/^sub_[a-zA-Z0-9_]+[.]vtt$/.test(s.url.slice(prefix.length)))) throw new Error('Invalid subtitle response.')
      return data
    },
    refetchOnWindowFocus: false,
    refetchInterval: q => q.state.error || q.state.data?.subtitles.length || Date.now() - started.current >= 60_000 ? false : 3000,
  })
  const subs = query.data?.subtitles ?? []
  if (!subs.length) return <span className="text-xs text-zinc-500">{query.isPending ? 'Checking subtitles…' : 'Subtitles unavailable'}</span>
  return <select aria-label="Subtitles" defaultValue="" onChange={e => onSelect(e.target.value || null)} className="bg-zinc-800 text-zinc-300 rounded p-1">
    <option value="">Subtitles off</option>
    {subs.map(s => <option key={s.url} value={s.url}>{s.language.toUpperCase()} {s.label}</option>)}
  </select>
}
