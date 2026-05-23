// Stub — vervang dit bestand door _webplayer/frontend/PlayerModal.tsx
// wanneer je de webplayer plugin activeert.
//
// Activeren:
//   1. cp _webplayer/frontend/PlayerModal.tsx  frontend/src/components/PlayerModal.tsx
//   2. cp _webplayer/frontend/SubtitlePicker.tsx frontend/src/components/SubtitlePicker.tsx
//   3. npm install hls.js  (in frontend/)
//   4. mkdir plugins/webplayer && cp _webplayer/* plugins/webplayer/
//   5. npm run build && herstart backend

export default function PlayerModal(_props: {
  imdb_id: string
  media_type: string
  title: string
  season?: number
  episode?: number
  onClose: () => void
}) {
  return null
}
