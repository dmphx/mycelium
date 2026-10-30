import { useEffect, useRef, useState } from 'react'
import Hls from 'hls.js'
import { useQuery } from '@tanstack/react-query'
import SubtitlePicker from './SubtitlePicker'
import { jsonRequest } from './requests'
import { api } from '../../api'

const csrfToken = () => document.querySelector<HTMLMetaElement>('meta[name="csrf-token"]')?.content || ''
const STEPS = ['searching', 'materializing', 'probing', 'preparing', 'ready', 'error']
interface Props { imdb_id: string; media_type: string; title: string; season?: number; episode?: number; onClose: () => void }
interface FileInfo { duration_s: number; video_codec: string; height: number; audio_tracks: { language: string; title: string }[] }
interface JobStatus { status: string; message: string; token?: string; stream_url?: string; stream_type?: 'direct' | 'hls'; file_info?: FileInfo; error?: string }
interface Playback { url: string; type: 'direct' | 'hls'; token: string; generation?: string; offset: number; duration: number; fileInfo?: FileInfo }

function checkedInfo(value: FileInfo | undefined): FileInfo | undefined {
  if (value === undefined) return undefined
  if (!value || typeof value !== 'object' || typeof value.video_codec !== 'string'
    || !Number.isFinite(value.duration_s) || value.duration_s < 0 || value.duration_s > 604800
    || !Array.isArray(value.audio_tracks) || value.audio_tracks.length > 32
    || value.audio_tracks.some(track => !track || typeof track.language !== 'string' || typeof track.title !== 'string')) throw new Error('Invalid playback metadata.')
  return value
}

function playback(url: unknown, token: string, offset = 0, duration = 0, fileInfo?: FileInfo): Playback {
  fileInfo = checkedInfo(fileInfo)
  if (!/^[a-f0-9]{32}$/.test(token) || typeof url !== 'string' || !Number.isFinite(offset) || offset < 0 || offset > 604800 || !Number.isFinite(duration) || duration < 0 || duration > 604800 || (duration > 0 && offset > duration)) throw new Error('Invalid playback response.')
  if (url === `/stream/${token}/direct`) return { url, token, type: 'direct', offset: 0, duration, fileInfo }
  const match = url.match(new RegExp(`^/stream/${token}/hls/([a-f0-9]{32})/(?:master|playlist)[.]m3u8$`))
  if (!match) throw new Error('Invalid playback response.')
  return { url, token, type: 'hls', generation: match[1], offset, duration, fileInfo }
}

export default function PlayerModal(props: Props) {
  const [attempt, setAttempt] = useState(0)
  const identity = JSON.stringify([props.imdb_id, props.media_type, props.season, props.episode, attempt])
  return <PlayerSession key={identity} {...props} onRetry={() => setAttempt(n => n + 1)} />
}

function PlayerSession({ imdb_id, media_type, title, season, episode, onClose, onRetry }: Props & { onRetry: () => void }) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const hlsRef = useRef<Hls | null>(null)
  const lifetime = useRef<AbortController>()
  const preparationDeadline = useRef(0)
  const [jobId, setJobId] = useState<string | null>(null)
  const [failure, setFailure] = useState<string | null>(null)
  const [view, setView] = useState<Playback | null>(null)
  const [converting, setConverting] = useState(false)
  const [playbackError, setPlaybackError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState(false)
  const [subtitleUrl, setSubtitleUrl] = useState<string | null>(null)
  const [jumpMin, setJumpMin] = useState('')
  const [jumping, setJumping] = useState(false)
  const [jumpError, setJumpError] = useState<string | null>(null)
  const { data: session } = useQuery({ queryKey: ['session'], queryFn: api.session })
  const traktEnabled = useRef(false)
  traktEnabled.current = !!(session?.user as any)?.trakt_connected

  useEffect(() => {
    const controller = new AbortController()
    lifetime.current = controller
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', escape)
    // Defer until after StrictMode's probe mount, so it cannot submit two jobs.
    const timer = setTimeout(async () => {
      if (media_type === 'tv' && (!Number.isInteger(season) || !Number.isInteger(episode) || season! < 0 || episode! < 1)) {
        setFailure('Choose a season and episode from Library to play this series.')
        return
      }
      try {
        const data = await jsonRequest('/ui/api/web-player/prepare', controller.signal, {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body: JSON.stringify({ imdb_id, media_type, season, episode }),
        })
        if (!data || typeof data !== 'object' || !/^[a-f0-9]{32}$/.test(data.job_id)) throw new Error('Invalid preparation response.')
        if (!controller.signal.aborted) { preparationDeadline.current = Date.now() + 180_000; setJobId(data.job_id) }
      } catch (error) {
        if (!controller.signal.aborted) setFailure(error instanceof Error ? error.message : 'Could not prepare playback.')
      }
    }, 0)
    return () => { clearTimeout(timer); controller.abort(); window.removeEventListener('keydown', escape) }
  }, [])

  const query = useQuery<JobStatus>({
    queryKey: ['wp-status', jobId], enabled: !!jobId && !failure, retry: false,
    queryFn: async ({ signal }) => {
      const data = await jsonRequest(`/ui/api/web-player/status/${jobId}`, signal)
      if (!data || typeof data !== 'object' || !STEPS.includes(data.status)
        || typeof data.message !== 'string' || (data.error != null && typeof data.error !== 'string')) throw new Error('Invalid player status response.')
      return data
    },
    refetchInterval: q => q.state.error || ['ready', 'error'].includes(q.state.data?.status || '') ? false : 800,
    refetchOnWindowFocus: false,
  })
  const status = query.data
  const error = failure || (query.error as Error | null)?.message || (status?.status === 'error' ? status.error || 'No compatible version found.' : null)
  useEffect(() => {
    if (!jobId || status?.status === 'ready' || status?.status === 'error') return
    const timer = setTimeout(() => setFailure('Playback preparation timed out. You can try again.'), Math.max(0, preparationDeadline.current - Date.now()))
    return () => clearTimeout(timer)
  }, [jobId, status?.status])
  useEffect(() => {
    if (status?.status !== 'ready') return
    try { setView(playback(status.stream_url, status.token || '', 0, status.file_info?.duration_s || 0, status.file_info)) }
    catch (error) { setFailure((error as Error).message) }
  }, [status?.status, status?.token, status?.stream_url])

  useEffect(() => {
    const video = videoRef.current
    if (!view || !video || error) return
    const controller = new AbortController()
    let disposed = false, convertingNow = false, saving = false, recovered = false
    let pollTimer: ReturnType<typeof setTimeout> | undefined
    let wakePoll: (() => void) | undefined
    const duration = () => view.duration > 0 ? view.duration : view.offset + video.duration
    const progress = () => duration() > 0 ? Math.min(100, (view.offset + video.currentTime) / duration() * 100) : 0
    const scrobble = (action: 'start' | 'stop' | 'pause') => {
      if (traktEnabled.current && Number.isFinite(progress())) api.traktScrobble({ action, media_type, imdb_id, progress: progress(), season, episode, title }).catch(() => {})
    }
    const convert = async () => {
      if (disposed || convertingNow || view.type !== 'direct') return
      convertingNow = true; setConverting(true)
      try {
        await jsonRequest(`/stream/${view.token}/convert-hls`, controller.signal, { method: 'POST', headers: { 'X-CSRFToken': csrfToken() } })
        const deadline = Date.now() + 180_000
        while (!controller.signal.aborted && Date.now() < deadline) {
          const data = await jsonRequest(`/stream/${view.token}/hls-status`, controller.signal)
          if (data.status === 'error') throw new Error('Conversion failed. Please reopen the player.')
          if (data.status === 'ready') {
            const next = playback(data.url, view.token, data.seek_offset_s, data.duration_s, data.file_info)
            if (!disposed) { setConverting(false); setView(next) }
            return
          }
          if (data.status !== 'converting') throw new Error('Invalid conversion response.')
          await new Promise<void>(resolve => { wakePoll = resolve; pollTimer = setTimeout(resolve, 800) })
        }
        if (!controller.signal.aborted) throw new Error('Conversion timed out. Please reopen the player.')
      } catch (error) {
        if (!disposed) { setConverting(false); setPlaybackError((error as Error).message) }
      }
    }
    const mediaError = () => {
      if (view.type === 'direct') void convert()
      else setPlaybackError('Playback failed. Please reopen the player.')
    }
    const play = () => { video.play().catch(() => {}) }
    const started = () => scrobble('start')
    video.addEventListener('loadedmetadata', play)
    video.addEventListener('error', mediaError)
    video.addEventListener('play', started)
    if (view.type === 'direct') video.src = view.url
    else if (Hls.isSupported()) {
      const hls = new Hls({ enableWorker: false, maxBufferLength: 60, maxMaxBufferLength: 120, maxBufferSize: 60_000_000 })
      hlsRef.current = hls
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal || disposed) return
        if (data.type === Hls.ErrorTypes.MEDIA_ERROR && !recovered) { recovered = true; hls.recoverMediaError() }
        else { setPlaybackError('Playback failed. Please reopen the player.'); hls.destroy() }
      })
      hls.attachMedia(video); hls.loadSource(view.url)
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) video.src = view.url
    else setPlaybackError('This browser cannot play this stream.')
    const saveTimer = setInterval(async () => {
      if (disposed || saving || video.paused || !Number.isFinite(video.currentTime) || !Number.isFinite(duration())) return
      saving = true
      try {
        await jsonRequest(`/stream/${view.token}/position`, controller.signal, {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() },
          body: JSON.stringify({ position_s: view.offset + video.currentTime, duration_s: duration(), generation: view.generation }),
        })
        if (!disposed) setSaveError(false)
      } catch { if (!disposed) setSaveError(true) }
      finally { saving = false }
    }, 10_000)
    return () => {
      disposed = true; controller.abort(); clearTimeout(pollTimer); wakePoll?.(); clearInterval(saveTimer)
      scrobble(progress() >= 80 ? 'stop' : 'pause')
      video.removeEventListener('loadedmetadata', play); video.removeEventListener('error', mediaError); video.removeEventListener('play', started)
      hlsRef.current?.destroy(); hlsRef.current = null
      video.pause(); video.removeAttribute('src'); video.load()
    }
  }, [view, error])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !subtitleUrl || !view || error) return
    const controller = new AbortController()
    let track: TextTrack | undefined
    const timer = setTimeout(() => controller.abort(), 15_000)
    fetch(subtitleUrl, { signal: controller.signal, credentials: 'same-origin', redirect: 'error' })
      .then(async r => { if (!r.ok) throw new Error(); return r.text() })
      .then(vtt => {
        if (controller.signal.aborted || vtt.length > 2_000_000) return
        track = video.addTextTrack('subtitles', 'external', 'und'); track.mode = 'showing'
        const seconds = (text: string) => text.trim().split(/\s/)[0].replace(',', '.').split(':').reduce((sum, part) => sum * 60 + Number(part), 0)
        let count = 0
        for (const block of vtt.split(/\r?\n\r?\n/)) {
          const lines = block.split(/\r?\n/), i = lines.findIndex(line => line.includes('-->'))
          if (i < 0 || ++count > 5000) continue
          const [a, b] = lines[i].split('-->'), start = seconds(a) - view.offset, end = seconds(b) - view.offset
          if (Number.isFinite(start) && Number.isFinite(end) && end > Math.max(0, start)) track.addCue(new VTTCue(Math.max(0, start), end, lines.slice(i + 1).join('\n')))
        }
      }).catch(() => {})
    return () => { clearTimeout(timer); controller.abort(); if (track) { track.mode = 'disabled'; for (const cue of Array.from(track.cues || [])) track.removeCue(cue) } }
  }, [subtitleUrl, view, error])

  const jump = async () => {
    if (!view || !lifetime.current || jumping) return
    const position = Number(jumpMin) * 60
    if (!jumpMin.trim() || !Number.isFinite(position) || position < 0 || position > 604800 || (view.duration > 0 && position > view.duration)) { setJumpError('Enter a minute within this video.'); return }
    setJumpError(null); setJumping(true)
    try {
      if (view.type === 'direct') {
        if (videoRef.current) videoRef.current.currentTime = position
      } else {
        const data = await jsonRequest(`/stream/${view.token}/seek`, lifetime.current.signal, {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken() }, body: JSON.stringify({ position_s: position }),
        }, 120_000)
        const next = playback(data.stream_url, view.token, data.seek_offset_s, data.duration_s, data.file_info)
        if (!lifetime.current.signal.aborted) { setSubtitleUrl(null); setView(next); setJumpMin('') }
      }
    } catch (error) { if (!lifetime.current.signal.aborted) setJumpError((error as Error).message) }
    finally { if (!lifetime.current.signal.aborted) setJumping(false) }
  }

  return <div role="dialog" aria-modal="true" aria-label={`Player: ${title}`} className="fixed inset-0 z-[300] bg-black/90 flex items-center justify-center p-4" onClick={e => e.target === e.currentTarget && onClose()}>
    <div className="relative w-full max-w-5xl bg-zinc-900 rounded-xl p-4" onClick={e => e.stopPropagation()}>
      <div className="flex justify-between gap-4 mb-3"><h2 className="text-white font-medium">{title}</h2><button onClick={onClose} aria-label="Close player" className="text-zinc-300">✕ Close</button></div>
      {error ? <div role="alert" className="p-8 text-center space-y-4"><p className="text-red-300">{error}</p><button onClick={onRetry} className="px-4 py-2 bg-zinc-700 rounded">Try again</button></div>
        : !view ? <p role="status" className="p-10 text-center text-zinc-300">{status?.message || 'Looking for a web-compatible version…'}</p>
        : <><video ref={videoRef} controls className="w-full aspect-video bg-black" crossOrigin="anonymous" />
          {converting && <p role="status">Switching to compatible mode…</p>}
          {playbackError && <p role="alert" className="text-red-300">{playbackError}</p>}
          <div className="flex items-center gap-3 flex-wrap text-sm text-zinc-300 mt-3">
            <SubtitlePicker key={`${view.token}:${view.generation}`} token={view.token} generation={view.generation} onSelect={setSubtitleUrl} />
            {(view.fileInfo?.audio_tracks?.length || 0) > 1 && view.type === 'hls' && <select aria-label="Audio track" onChange={e => { if (hlsRef.current) hlsRef.current.audioTrack = Number(e.target.value) }} className="bg-zinc-800 rounded p-1">
              {view.fileInfo!.audio_tracks.map((track, i) => <option key={i} value={i}>{track.language?.toUpperCase()} {track.title}</option>)}
            </select>}
            <label className="ml-auto">Jump to minute <input aria-label="Jump to minute" type="number" min="0" value={jumpMin} onChange={e => setJumpMin(e.target.value)} onKeyDown={e => e.key === 'Enter' && void jump()} className="w-20 bg-zinc-800 rounded p-1" /></label>
            <button onClick={() => void jump()} disabled={jumping || !jumpMin} className="bg-zinc-700 px-3 py-1 rounded disabled:opacity-40">{jumping ? 'Seeking…' : 'Jump'}</button>
          </div>
          {jumpError && <p role="alert" className="text-red-300">{jumpError}</p>}
          {saveError && <p role="status">Progress could not be saved.</p>}
        </>}
    </div>
  </div>
}
